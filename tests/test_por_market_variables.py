"""Synthetic production/use, membrane attribution and utility-balance tests."""
from copy import deepcopy
from contextlib import ExitStack, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import importlib.util
import unittest
import numpy as np
import pandas as pd
from por_market_variables import extend_port_market_variables


def fixture():
    cols=['LOCATION','TECHNOLOGY','PRODUCT','MODE_OF_OPERATION','YEAR']
    sources={}
    for scenario in ('CL','OCE'):
        production=[];use=[];imports=[];exports=[]
        for year in (2035,2050):
            def row(t,p,q,m=1):return ['Rotterdam',t,p,m,year,q]
            production.extend([row('MTO_with_loop','steam',100),row('MTO_with_loop','HT_heat',50),
                row('naphtha_steam_cracker_default_default','cracker_H2',300 if scenario=='CL' else 400),
                row('hydrogen_separation_membrane','hydrogen',180)])
            if scenario=='CL':production.append(row('retrofit_MTA_with_loop','cracker_H2',100))
            use.extend([row('MTO_with_loop','steam',30),row('MTO_with_loop','HT_heat',75),
                        row('outside_process','steam',40),row('waste_production_demo','steam',30),
                        row('hydrogen_separation_membrane','cracker_H2',200),
                        row('plastic_waste_pyrolysis_demo','methane',10)])
            imports.extend([['steam',year,10],['HT_heat',year,25]])
            exports.append(['steam',year,10])
        sources[scenario]=dict(production_volume=pd.DataFrame(production,columns=cols+['LocalProductionByMode']),
            use_volume=pd.DataFrame(use,columns=cols+['LocalUseByMode']),
            transport_to_PoR=pd.DataFrame(imports,columns=['PRODUCT','YEAR','Transport']),
            transport_from_PoR=pd.DataFrame(exports,columns=['PRODUCT','YEAR','Transport']))
    existing=pd.DataFrame([dict(model='Petchem',scenario=s,region='NL',variables=v,unit=u,**{'2035':q,'2050':q})
        for s in sources for v,u,q in [('Energy Input|steam|MTO_with_loop','TJ/yr',20),
                                     ('Material Input|hydrogen|hydrogen_separation_membrane','kt/yr',36)]])
    return existing,sources


class PortMarketVariables(unittest.TestCase):
    def test_all_port_steam_and_signed_outside_supply(self):
        old,sources=fixture()
        # An existing port producer operates only in CL, only in 2035.
        p=sources['CL']['production_volume']
        p.loc[len(p)]=['Rotterdam','legacy_factory','steam',3,2035,25]
        result=extend_port_market_variables(old,sources)
        data=result['data'].set_index(['scenario','variables'])
        self.assertEqual(data.loc[('CL','Energy Use|steam|outside_process|mode1'),2035],8)
        self.assertEqual(data.loc[('CL','Energy Output|steam|legacy_factory|mode3'),2035],5)
        self.assertEqual(data.loc[('CL','Energy Output|steam|legacy_factory|mode3'),2050],0)
        self.assertEqual(data.loc[('OCE','Energy Output|steam|legacy_factory|mode3'),2035],0)
        self.assertFalse(any('waste_production_demo' in x for x in result['data'].variables))
        for scenario in ('CL','OCE'):
            for year in (2035,2050):
                part=result['data'].loc[result['data'].scenario.eq(scenario)]
                use=part.loc[part.variables.str.match(r'Energy Use\|steam\|.*\|mode\d+$'),year].sum()
                production=part.loc[part.variables.str.match(r'Energy Output\|steam\|.*\|mode\d+$'),year].sum()
                value=data.loc[(scenario,'Energy Supply|steam|outside_port'),year]
                self.assertAlmostEqual(production+value,use)
                self.assertLess(value,0)  # Surplus remains negative, not clipped.
        audit=result['row_directions'].loc[lambda f:f.derived_outside_supply]
        self.assertTrue(audit.direction.eq('supply').all())
        self.assertTrue(audit.negative_years.map(bool).all())

    def test_steam_outside_supply_can_be_positive_or_zero(self):
        old,sources=fixture()
        for scenario in sources:
            frame=sources[scenario]['production_volume']
            frame.loc[frame.PRODUCT.eq('steam'),'LocalProductionByMode']=70 if scenario=='CL' else 0
            old.loc[old.scenario.eq(scenario)&old.variables.eq('Energy Input|steam|MTO_with_loop'),['2035','2050']]=14 if scenario=='CL' else 0
        data=extend_port_market_variables(old,sources)['data'].set_index(['scenario','variables'])
        self.assertEqual(data.loc[('CL','Energy Supply|steam|outside_port'),2035],0)
        self.assertEqual(data.loc[('OCE','Energy Supply|steam|outside_port'),2035],14)

    def test_converter_with_new_market_yaml_resolves_before_generic_completion(self):
        """Real converter + exporter + completion, synthetic workbook/YAML only."""
        import yaml
        root=Path(__file__).resolve().parents[1]
        spec=importlib.util.spec_from_file_location('port_integration_converter',root/'0_convert_ITOM_por_to_iamc.py')
        converter=importlib.util.module_from_spec(spec);spec.loader.exec_module(converter)
        _,sources=fixture()
        for sheets in sources.values():
            f=sheets['production_volume'];f.loc[f.TECHNOLOGY.eq('MTO_with_loop'),'MODE_OF_OPERATION']=2
            sheets['products']=pd.DataFrame({'PRODUCT':['steam','HT_heat','hydrogen','cracker_H2','methane'],
                'type':['energy input','energy input','material input','intermediates, by-products','intermediates, by-products']})
            sheets['input_factors']=sheets['output_factors']=pd.DataFrame()
        variables=['Energy Output|steam|MTO_with_loop|mode2',
                   'Energy Output|steam|retrofit_MTA_with_loop|mode1',
                   'Intermediate Product_by_product|methane|retrofit_MTA_with_loop|mode2',
                   'Energy Use|steam|outside_process|mode1',
                   'Energy Supply|steam|outside_port',
                   'Material Input|hydrogen|hydrogen_separation_membrane|from|retrofit_MTA_with_loop|mode1']
        config={'production pathways':{str(i):{'production volume':{'variable':v}} for i,v in enumerate(variables)}}
        config['production pathways']['FE_demo']={'production volume':{'variable':'Final Product|test|route'}}
        with TemporaryDirectory() as tmp,ExitStack() as stack,redirect_stdout(StringIO()):
            folder=Path(tmp);paths={s:folder/(s+'.xlsx') for s in sources}
            for p in paths.values():p.write_bytes(b'synthetic-workbook')
            path=folder/'markets.yaml';path.write_text(yaml.safe_dump(config))
            stack.enter_context(patch.object(converter.pd,'read_excel',side_effect=lambda p,**kw:sources[Path(p).stem]))
            for name in ('verify_px_extraction_yield','validate_px_source_flows'):
                stack.enter_context(patch.object(converter,name))
            stack.enter_context(patch.object(converter,'annual_raw_px_use',return_value=pd.DataFrame()))
            stack.enter_context(patch.object(converter,'reconcile_px_terminal_purity',side_effect=lambda f,_:(f,[])))
            result=converter.main(config_path=path,scenario_inputs=paths,port_market_flows=True,write=False)
        data=result['data'].set_index(['scenario','variables'])
        self.assertEqual(data.loc[('CL',variables[0]),2035],20)
        self.assertEqual(data.loc[('OCE',variables[0]),2050],20)
        self.assertEqual(data.loc[('CL',variables[-1]),2050],9)
        self.assertEqual(data.loc[('OCE',variables[-1]),2050],0)
        self.assertEqual(data.loc[('CL','Energy Use|steam|outside_process|mode1'),2035],8)
        self.assertEqual(data.loc[('CL','Energy Supply|steam|outside_port'),2035],-6)
        self.assertFalse(result['data'].duplicated(['model','scenario','region','variables','unit']).any())
        self.assertFalse(any(r['variable'] in variables for r in result['audit_data']['explicit_zero_pathways']))

    def test_resolved_variable_missing_in_one_context_fails_without_zero_fill(self):
        import importlib.util,yaml
        root=Path(__file__).resolve().parents[1]
        spec=importlib.util.spec_from_file_location('resolved_converter',root/'0_convert_ITOM_por_to_iamc.py')
        c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
        existing,sources=fixture();data=extend_port_market_variables(existing,sources)['data']
        v='Energy Output|steam|MTO_with_loop|mode1'
        data=data.loc[~(data.scenario.eq('OCE')&data.variables.eq(v))]
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/'config.yaml';path.write_text(yaml.safe_dump({'production pathways':{'x':{'production volume':{'variable':v}}}}))
            for function in (lambda:c.add_mode_aware_rows(data,data.iloc[:0],path,resolved_variables={v}),
                             lambda:c.complete_absent_pathways(data,path,resolved_variables={v})):
                with self.assertRaisesRegex(ValueError,'resolved port variable'):function()

    def test_annualization_modes_existing_values_and_immutable_sources(self):
        old,sources=fixture();before=deepcopy(sources);r=extend_port_market_variables(old,sources)
        for scenario in sources:
            for sheet in sources[scenario]:pd.testing.assert_frame_equal(sources[scenario][sheet],before[scenario][sheet])
        f=r['data'].set_index(['scenario','variables'])
        self.assertEqual(f.loc[('CL','Energy Output|steam|MTO_with_loop|mode1'),2035],20)
        self.assertEqual(f.loc[('CL','Energy Use|steam|MTO_with_loop|mode1'),2035],6)
        self.assertEqual(f.loc[('CL','Energy Input|steam|MTO_with_loop'),2035],20)
        self.assertFalse(r['data'].duplicated(['model','scenario','region','variables','unit']).any())

    def test_proportional_membrane_input_and_output_close(self):
        old,sources=fixture();r=extend_port_market_variables(old,sources);f=r['hydrogen_split']
        for (scenario,year),part in f.groupby(['scenario','year']):
            self.assertAlmostEqual(part.source_share.sum(),1)
            self.assertAlmostEqual(part.hydrogen_output_attributed.sum(),36)
            self.assertAlmostEqual(part.cracker_H2_to_membrane.sum(),40)
            mta=part.loc[part.source_technology.eq('retrofit_MTA_with_loop')]
            self.assertAlmostEqual(mta.hydrogen_output_attributed.sum(),9 if scenario=='CL' else 0)
        data=r['data'];variable='Material Input|hydrogen|hydrogen_separation_membrane|from|retrofit_MTA_with_loop|mode1'
        self.assertEqual(data.loc[data.scenario.eq('CL')&data.variables.eq(variable),2035].iloc[0],9)

    def test_absent_pathway_zero_and_mode_totals(self):
        old,sources=fixture();r=extend_port_market_variables(old,sources);f=r['data']
        for scenario in ('CL','OCE'):
            for base in ('Intermediate Product_by_product|cracker_H2|retrofit_MTA_with_loop','Energy Output|steam|MTO_with_loop'):
                part=f.loc[f.scenario.eq(scenario)]
                aggregate=part.loc[part.variables.eq(base),[2035,2050]].to_numpy()
                modes=part.loc[part.variables.str.startswith(base+'|mode'),[2035,2050]].sum().to_numpy()
                np.testing.assert_allclose(aggregate[0],modes)
        self.assertFalse(r['explicit_zeros'].empty)

    def test_physical_balance_keeps_pricing_exclusion_and_surplus_visible(self):
        old,sources=fixture();r=extend_port_market_variables(old,sources)
        steam=r['energy_balance'].loc[lambda f:f['product'].eq('steam')]
        np.testing.assert_allclose(steam.excluded_pricing_use,6)
        np.testing.assert_allclose(steam.physical_port_remainder,-6)
        np.testing.assert_allclose(steam.balance_with_pricing,0)
        self.assertFalse(steam.approved_for_market_use.any())
        self.assertTrue(r['flow_details'].loc[lambda f:f.excluded_pricing,'source_row'].ge(2).all())

    def test_every_legacy_input_and_new_use_has_a_direction(self):
        old,sources=fixture();r=extend_port_market_variables(old,sources);audit=r['row_directions']
        self.assertEqual(len(audit),len(r['data']))
        self.assertTrue(audit.loc[audit.variables.str.startswith(('Energy Input|','Material Input|')),'direction'].eq('production').all())
        self.assertTrue(audit.loc[audit.variables.str.startswith(('Energy Use|','Material Use|')),'direction'].eq('use').all())

    def test_missing_columns_bad_values_and_changed_existing_rows_fail(self):
        for error in ('column','nan','negative','mode','existing'):
            with self.subTest(error=error):
                old,sources=fixture()
                f=sources['CL']['production_volume']
                if error=='column':sources['CL']['production_volume']=f.drop(columns='MODE_OF_OPERATION')
                elif error=='nan':f.loc[0,'LocalProductionByMode']=np.nan
                elif error=='negative':f.loc[0,'LocalProductionByMode']=-1
                elif error=='mode':f.loc[0,'MODE_OF_OPERATION']=3
                else:old.loc[0,'2035']=99
                with self.assertRaises(ValueError):extend_port_market_variables(old,sources)

    def test_membrane_without_producers_or_excess_demand_fails(self):
        for no_supply in (False,True):
            old,sources=fixture();f=sources['CL']['production_volume'];f.loc[f.PRODUCT.eq('cracker_H2'),'LocalProductionByMode']=0 if no_supply else 1
            with self.assertRaisesRegex(ValueError,'membrane|Membrane'):extend_port_market_variables(old,sources)

    def test_unreviewed_positive_cracker_hydrogen_source_fails(self):
        old,sources=fixture()
        f=sources['CL']['production_volume']
        f.loc[f.TECHNOLOGY.eq('retrofit_MTA_with_loop'),'TECHNOLOGY']='unreviewed_source'
        with self.assertRaisesRegex(ValueError,'Unreviewed cracker-H2 producer'):extend_port_market_variables(old,sources)
