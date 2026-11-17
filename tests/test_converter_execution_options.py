"""Converter I/O contract checks; no private workbooks or model construction."""
from contextlib import ExitStack, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import importlib.util
import inspect
import json
import unittest

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('converter_options', ROOT / '0_convert_ITOM_por_to_iamc.py')
converter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(converter)


class ExecutionOptions(unittest.TestCase):
    def test_disabled_port_flows_never_call_extension(self):
        with TemporaryDirectory() as tmp,patch('por_market_variables.extend_port_market_variables',
                side_effect=AssertionError('Disabled extension was called')):
            default,_,_=self.run_fixture(Path(tmp),write=False)
            disabled,_,_=self.run_fixture(Path(tmp),write=False,port_market_flows=False)
            pd.testing.assert_frame_equal(default['data'],disabled['data'])

    def test_market_flow_files_are_tagged_inside_selected_destination(self):
        def extend(frame,sources):
            return {'data':frame,'row_directions':pd.DataFrame([{'direction':'production'}])}
        with TemporaryDirectory() as tmp,patch('por_market_variables.extend_port_market_variables',side_effect=extend):
            destination=Path(tmp)/'outputs'
            result,_,_=self.run_fixture(Path(tmp),port_market_flows=True,output_dir=destination,output_tag='check')
            path=result['market_tables']['row_directions']
            self.assertEqual(path,destination.resolve()/'por_market_row_directions_check.csv')
            self.assertTrue(path.is_file())

    def test_market_flow_option_returns_tables_and_audit_without_writes(self):
        calls=[]
        def extend(frame,sources):
            calls.append(set(sources))
            return {'data':frame,'row_directions':pd.DataFrame([{'direction':'production'}])}
        with TemporaryDirectory() as tmp,patch('por_market_variables.extend_port_market_variables',side_effect=extend):
            destination=Path(tmp)/'never-written'
            result,_,_=self.run_fixture(Path(tmp),write=False,port_market_flows=True,output_dir=destination)
            self.assertEqual(calls,[{'CL','OCE'}])
            self.assertTrue(result['audit_data']['port_market_flows']['balance_review_required'])
            self.assertIn('row_directions',result['market_tables'])
            self.assertFalse(destination.exists())

    def run_fixture(self, root, **options):
        data = root / 'scenario_data'
        data.mkdir(exist_ok=True)
        for name in converter.SCENARIO_INPUTS.values():
            (data / name).write_bytes(b'synthetic source fingerprint')
        config = root / 'active.yaml'
        config.write_text('production pathways: {}\n')
        (root / 'datapackages').mkdir(exist_ok=True)
        (root / 'datapackages/datapackage_itom_por.json').write_text(json.dumps(
            {'resources': [{'name': 'config', 'path': 'active.yaml'}]}))
        sheets = {'products': pd.DataFrame({'PRODUCT': ['test'], 'type': ['Final Product']})}
        sheets.update({name: pd.DataFrame() for name in (
            'production_volume', 'transport_to_PoR', 'transport_from_PoR',
            'use_volume', 'input_factors', 'output_factors')})
        def production(*args, **kwargs):
            return pd.DataFrame([{'model': 'Petchem', 'scenario': kwargs['scenario'],
                'region': 'NL', 'variables': 'Final Product|test|route', 'unit': 'kt/yr', '2050': 1.}])
        with ExitStack() as stack, redirect_stdout(StringIO()):
            stack.enter_context(patch.object(converter, '__file__', str(root / 'converter.py')))
            reader = stack.enter_context(patch.object(converter.pd, 'read_excel', return_value=sheets))
            for name in ('verify_px_extraction_yield', 'validate_px_source_flows'):
                stack.enter_context(patch.object(converter, name))
            stack.enter_context(patch.object(converter, 'annual_raw_px_use', return_value=pd.DataFrame()))
            stack.enter_context(patch.object(converter, 'to_iamc_production_volume', side_effect=production))
            stack.enter_context(patch.object(converter, 'to_iamc_trade_volume', return_value=production(scenario='CL').iloc[:0]))
            stack.enter_context(patch.object(converter, 'reconcile_px_terminal_purity', side_effect=lambda frame, _: (frame, [])))
            for name in ('add_mode_aware_rows', 'add_ccs_aggregates', 'ensure_required_fe_variables'):
                stack.enter_context(patch.object(converter, name, side_effect=lambda frame, *args, **kwargs: frame))
            complete = stack.enter_context(patch.object(converter, 'complete_absent_pathways', side_effect=lambda frame, _, **kwargs: (frame, [])))
            result = converter.main(**options)
        return result, reader, complete

    def test_defaults_keep_mapping_completion_and_output_names(self):
        self.assertIs(inspect.signature(converter.main).parameters['complete_absent'].default, True)
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            result, _, complete = self.run_fixture(root)
            self.assertEqual(complete.call_count, 1)
            self.assertEqual(result['combined'], root / 'scenario_data/scenario_data_itom_por.csv')
            audit = json.loads(result['audit'].read_text())
            self.assertEqual(set(audit['source_sha256']), set(converter.SCENARIO_INPUTS.values()))

    def test_read_only_returns_data_audit_and_writes_no_destination(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            dest = root / 'must_not_exist'
            result, _, _ = self.run_fixture(root, output_dir=dest, write=False)
            self.assertFalse(dest.exists())
            self.assertEqual(set(result['data'].scenario), {'CL', 'OCE'})
            self.assertTrue(all(Path(p).is_absolute() for p in result['audit_data']['source_sha256']))
            self.assertFalse(list((root / 'scenario_data').glob('*.csv')))

    def test_override_sources_destination_tag_and_completion(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / 'external'
            source.mkdir()
            paths = {s: source / (s + '.xlsx') for s in converter.SCENARIO_INPUTS}
            for path in paths.values(): path.write_bytes(b'custom')
            result, reader, complete = self.run_fixture(root, scenario_inputs=paths,
                output_dir=root/'outputs', output_tag='separate', complete_absent=False)
            self.assertEqual([call.args[0] for call in reader.call_args_list], list(paths.values()))
            self.assertFalse(complete.called)
            self.assertEqual(result['combined'].name, 'scenario_data_itom_por_separate.csv')
            self.assertEqual(set(json.loads(result['audit'].read_text())['source_sha256']), set(map(str, paths.values())))

    def test_partial_scenario_override_fails(self):
        with self.assertRaisesRegex(ValueError, 'exactly the CL and OCE'):
            converter.main(scenario_inputs={'OCE': 'missing.xlsx'}, write=False)


if __name__ == '__main__': unittest.main()
