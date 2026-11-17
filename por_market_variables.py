"""Optional annual PoR market quantities and review-only physical balances.

Directions describe physical flows, independent of legacy ITOM classifications.
No market is created and no balance residual is assigned to a supplier here.
Steam mode rows cover all recorded port users and producers, excluding pricing
use. Energy Supply|steam|outside_port is real use minus production, retains
negative values, and is independent of recorded imports and exports.
"""
import re
import numpy as np
import pandas as pd

PRODUCTS = {'hydrogen', 'cracker_H2', 'methane', 'steam', 'HT_heat'}
ENERGY = {'steam', 'HT_heat'}
HYDROGEN_PRODUCERS = {'chlorine_production_BAT', 'styrene_default', 'toluene_extraction',
                      'hydrogen_separation_membrane', 'hydrogen_supply'}
CRACKER_PRODUCERS = {'retrofit_MTA_with_loop', 'naphtha_steam_cracker_default_default',
                     'retrofit_naphtha_cracker', 'retrofit_gasoil_cracker'}


def own_technology(name):
    return name in {'MTO_with_loop', 'retrofit_MTA_with_loop', 'methanol_synthesis_from_syngas',
                    'gasification_of_cracker_HFO'} or name.startswith(
                        ('gasification_of_plastic_waste_', 'plastic_waste_pyrolysis_'))


def _required(frame, columns, label):
    missing = set(columns) - set(frame)
    if missing:
        raise ValueError(f'{label}: missing columns {sorted(missing)}')


def _quantities(frame, scenario, sheet, annualization):
    amount = 'LocalProductionByMode' if sheet == 'production_volume' else 'LocalUseByMode'
    _required(frame, ['LOCATION', 'TECHNOLOGY', 'PRODUCT', 'MODE_OF_OPERATION', 'YEAR', amount], sheet)
    f = frame.loc[frame.LOCATION.isin(['Rotterdam', 'NL']) & frame.PRODUCT.isin(PRODUCTS)].copy()
    for col in ('YEAR', 'MODE_OF_OPERATION', amount):
        f[col] = pd.to_numeric(f[col], errors='raise')
        if not np.isfinite(f[col]).all():
            raise ValueError(f'{sheet}: non-finite {col}')
    if (f[amount] < 0).any() or (f.MODE_OF_OPERATION < 1).any() or not (f.MODE_OF_OPERATION % 1 == 0).all() or not (f.YEAR % 1 == 0).all():
        raise ValueError(f'{sheet}: negative quantity or invalid year/mode')
    return pd.DataFrame(dict(scenario=scenario, technology=f.TECHNOLOGY, product=f.PRODUCT,
        mode=f.MODE_OF_OPERATION.astype(int), year=f.YEAR.astype(int), amount=f[amount]/annualization,
        unit=f.PRODUCT.map(lambda p:'TJ/yr' if p in ENERGY else 'kt/yr'),
        direction='production' if sheet=='production_volume' else 'use',
        source_sheet=sheet, source_row=f.index+2,
        own=f.TECHNOLOGY.map(own_technology),
        excluded_pricing=f.TECHNOLOGY.str.startswith('waste_production_') & f.PRODUCT.eq('steam') & (sheet=='use_volume')))


def _variable(product, technology, mode, direction):
    if direction == 'use':
        prefix = 'Energy Use' if product in ENERGY else 'Material Use'
    elif product in ENERGY:
        prefix = 'Energy Output'
    else:
        prefix = 'Material Input' if product == 'hydrogen' else 'Intermediate Product_by_product'
    return f'{prefix}|{product}|{technology}|mode{mode}'


def extend_port_market_variables(existing, sources, *, annualization=5):
    """Return additive IAMC rows, directions, producer/consumer data and balances.

    ``sources`` maps scenario to the four original production/use/trade sheets.
    Missing pathways are zero-filled from the union of source technologies and
    modes. Present-but-invalid cells are errors, never converted to zeros.
    """
    if not np.isfinite(annualization) or annualization <= 0:
        raise ValueError('Annualization must be finite and positive')
    years=sorted(int(c) for c in existing if str(c).isdigit())
    scenarios=sorted(sources)
    if set(existing.scenario) != set(scenarios) or not years:
        raise ValueError('Market-variable contexts differ from the base conversion')
    details=[];tech_modes=set();trade=[]
    for scenario, sheets in sources.items():
        for sheet in ('production_volume','use_volume'):
            f=sheets[sheet];details.append(_quantities(f,scenario,sheet,annualization))
            for row in f.loc[f.LOCATION.isin(['Rotterdam','NL']),['TECHNOLOGY','MODE_OF_OPERATION']].itertuples(index=False):
                if own_technology(row.TECHNOLOGY):
                    mode=float(row.MODE_OF_OPERATION)
                    if not np.isfinite(mode) or mode < 1 or mode % 1:
                        raise ValueError('Invalid own-technology mode')
                    if row.TECHNOLOGY in ('MTO_with_loop','retrofit_MTA_with_loop') and mode not in (1,2):
                        raise ValueError('Unreviewed MTO/MTA operating mode')
                    tech_modes.add((row.TECHNOLOGY,int(mode)))
        for sheet,direction,location in [('transport_to_PoR','import','LOCATION_2'),('transport_from_PoR','export','LOCATION_1')]:
            f=sheets[sheet];_required(f,['PRODUCT','YEAR','Transport'],sheet)
            f=f.loc[f.PRODUCT.isin(PRODUCTS)].copy()
            if location in f:f=f.loc[f[location].isin(['Rotterdam','NL'])]
            for col in ('YEAR','Transport'):
                f[col]=pd.to_numeric(f[col],errors='raise')
                if not np.isfinite(f[col]).all():raise ValueError(f'{sheet}: non-finite {col}')
            if (f.Transport<0).any() or not (f.YEAR%1==0).all():raise ValueError(f'{sheet}: invalid quantity/year')
            trade.extend(dict(scenario=scenario,year=int(r.YEAR),product=r.PRODUCT,direction=direction,amount=float(r.Transport)/annualization) for r in f.itertuples())
    detail=pd.concat(details,ignore_index=True)
    if set(detail.year)-set(years):raise ValueError('Source flow years absent from base conversion')
    uncovered=detail.loc[detail.direction.eq('production') & detail['product'].eq('cracker_H2')
                         & detail.amount.gt(0) & ~detail.technology.isin(CRACKER_PRODUCERS)]
    if not uncovered.empty:
        raise ValueError(f'Unreviewed cracker-H2 producer: {sorted(set(uncovered.technology))}')
    for t in ('MTO_with_loop','retrofit_MTA_with_loop'):
        tech_modes.update((t,m) for m in (1,2))
    index=detail.groupby(['scenario','year','direction','product','technology','mode']).amount.sum().to_dict()
    keys=set()
    for t,m in tech_modes:
        for product in ENERGY:
            keys.update((direction,product,t,m) for direction in ('production','use'))
        if t.startswith('plastic_waste_pyrolysis_'):keys.add(('use','methane',t,m))
    for r in detail.itertuples():
        # Steam serves the whole port. Pricing devices remain audit-only.
        if r.product == 'steam' and not r.excluded_pricing:
            keys.add((r.direction,r.product,r.technology,r.mode))
        if r.direction=='production' and ((r.product=='hydrogen' and r.technology in HYDROGEN_PRODUCERS) or (r.product=='cracker_H2' and r.technology in CRACKER_PRODUCERS) or (r.product=='methane' and r.technology=='retrofit_MTA_with_loop')):
            keys.add((r.direction,r.product,r.technology,r.mode))
        if r.direction=='use' and r.product in ('hydrogen','cracker_H2'):
            keys.add((r.direction,r.product,r.technology,r.mode))
    keys.update(('production','hydrogen',t,1) for t in HYDROGEN_PRODUCERS)
    keys.update(('production',p,'retrofit_MTA_with_loop',m) for p in ('cracker_H2','methane') for m in (1,2))
    for t in CRACKER_PRODUCERS-{'retrofit_MTA_with_loop'}:keys.add(('production','cracker_H2',t,1))
    rows=[];zero=[]
    for scenario in scenarios:
        models=existing.loc[existing.scenario.eq(scenario)&existing.region.eq('NL'),'model'].unique()
        if len(models)!=1:raise ValueError('Expected one NL model context per scenario')
        for direction,product,t,m in sorted(keys):
            row=dict(model=models[0],scenario=scenario,region='NL',variables=_variable(product,t,m,direction),unit='TJ/yr' if product in ENERGY else 'kt/yr')
            for year in years:
                k=(scenario,year,direction,product,t,m);row[year]=index.get(k,0.)
                if k not in index:zero.append(dict(scenario=scenario,year=year,variables=row['variables'],reason='absent source row; explicit pathway zero'))
            rows.append(row)
    added=pd.DataFrame(rows)
    # Preserve an aggregate as well as its mode detail, without adding either
    # twice to physical balances (which always use original source rows).
    aggregate=added.copy();aggregate['variables']=aggregate.variables.str.replace(r'\|mode\d+$','',regex=True)
    meta=['model','scenario','region','variables','unit']
    added=pd.concat([added,aggregate.groupby(meta,as_index=False)[years].sum()],ignore_index=True)
    split=[];derived=[]
    producers=sorted((t,m) for d,p,t,m in keys if d=='production' and p=='cracker_H2')
    for scenario in scenarios:
        model=existing.loc[existing.scenario.eq(scenario),'model'].iloc[0]
        for year in years:
            def total(direction,product,t):return sum(v for (s,y,d,p,tech,m),v in index.items() if (s,y,d,p,tech)==(scenario,year,direction,product,t))
            steam_production = sum(q for (s,y,d,p,t,m),q in index.items()
                                   if (s,y,d,p)==(scenario,year,'production','steam'))
            real_steam_use = sum(q for (s,y,d,p,t,m),q in index.items()
                                 if (s,y,d,p)==(scenario,year,'use','steam')
                                 and not t.startswith('waste_production_'))
            derived.append(dict(model=model,scenario=scenario,region='NL',
                variables='Energy Supply|steam|outside_port',unit='TJ/yr',year=year,
                value=real_steam_use-steam_production))
            demand=total('use','cracker_H2','hydrogen_separation_membrane')
            output=total('production','hydrogen','hydrogen_separation_membrane')
            supply=sum(index.get((scenario,year,'production','cracker_H2',t,m),0.) for t,m in producers)
            if (demand>0 or output>0) and supply<=0:raise ValueError(f'No cracker-H2 source for membrane: {scenario} {year}')
            if demand>supply+max(1e-8,1e-8*supply):raise ValueError(f'Membrane cracker-H2 demand exceeds port production: {scenario} {year}')
            for t,m in producers:
                q=index.get((scenario,year,'production','cracker_H2',t,m),0.);share=q/supply if supply else 0.
                split.append(dict(scenario=scenario,year=year,source_technology=t,source_mode=m,cracker_H2_production=q,source_share=share,membrane_cracker_H2_demand=demand,cracker_H2_to_membrane=demand*share,membrane_hydrogen_output=output,hydrogen_output_attributed=output*share,unit='kt/yr',assumption='proportional mixing by source cracker_H2 production'))
                for product,direction,value in [('hydrogen','production',output*share),('cracker_H2','use',demand*share)]:
                    prefix='Material Input' if direction=='production' else 'Material Use'
                    derived.append(dict(model=model,scenario=scenario,region='NL',variables=f'{prefix}|{product}|hydrogen_separation_membrane|from|{t}|mode{m}',unit='kt/yr',year=year,value=value))
    derived=pd.DataFrame(derived).pivot(index=meta,columns='year',values='value').reset_index()
    added=pd.concat([added,derived],ignore_index=True)
    # Existing classifications are retained as aliases, not renamed.
    old=existing.copy();old.columns=[int(c) if str(c).isdigit() else c for c in old]
    old_index=old.set_index(meta)
    additions=[]
    for row in added.to_dict('records'):
        k=tuple(row[c] for c in meta)
        if k in old_index.index:
            values=old_index.loc[k,years]
            if isinstance(values,pd.DataFrame) or not np.allclose(values.astype(float),[row[y] for y in years],rtol=1e-10,atol=1e-8):raise ValueError(f'Existing production row differs: {k}')
        else:additions.append(row)
    combined=pd.concat([old,pd.DataFrame(additions)],ignore_index=True)
    if combined.duplicated(meta).any():raise ValueError('Duplicate market-variable identity')
    directions=[]
    for row in combined.to_dict('records'):
        parts=row['variables'].split('|');trade_row=parts[0]=='Trade'
        product=parts[2] if trade_row else parts[1]
        if product not in PRODUCTS:continue
        outside_supply = row['variables']=='Energy Supply|steam|outside_port'
        direction='supply' if outside_supply else ('production' if parts[1]=='To PoR' else 'use') if trade_row else 'use' if parts[0] in ('Material Use','Energy Use') else 'production'
        derived_row='|from|' in row['variables']
        source=('transport_to_PoR' if direction=='production' else 'transport_from_PoR') if trade_row else 'use_volume' if direction=='use' else 'production_volume'
        if outside_supply:source='derived: real use_volume minus production_volume; pricing excluded'
        if row['region']=='NL' and not trade_row and not derived_row and not outside_supply:
            technology=parts[2]
            mode=int(parts[-1][4:]) if re.fullmatch(r'mode\d+',parts[-1]) else None
            for year in years:
                value=sum(q for (s,y,d,p,t,m),q in index.items()
                          if (s,y,d,p,t)==(row['scenario'],year,direction,product,technology) and (mode is None or m==mode))
                if not np.isclose(float(row[year]),value,rtol=1e-10,atol=1e-8):
                    raise ValueError(f'Production/use row differs from its source: {row["variables"]}, {year}')
        directions.append(dict(scenario=row['scenario'],region=row['region'],variables=row['variables'],unit=row['unit'],direction=direction,source_sheet=source,derived_proportional_split=derived_row,derived_outside_supply=outside_supply,negative_years=[y for y in years if float(row[y])<0] if outside_supply else [],legacy_Input_name_holds_production=direction=='production' and parts[0] in ('Material Input','Energy Input'),counting_note='Alias/mode/detail rows must not be summed together; balances use source rows only; outside supply is signed, not clipped'))
    energy=[];trade=pd.DataFrame(trade,columns=['scenario','year','product','direction','amount'])
    for scenario in scenarios:
        for year in years:
            for product in sorted(ENERGY):
                f=detail.loc[detail.scenario.eq(scenario)&detail.year.eq(year)&detail['product'].eq(product)]
                def q(direction,own=None,pricing=False):
                    mask=f.direction.eq(direction)&f.excluded_pricing.eq(pricing)
                    if own is not None:mask &= f.own.eq(own)
                    return float(f.loc[mask,'amount'].sum())
                tf=trade.loc[trade.scenario.eq(scenario)&trade.year.eq(year)&trade['product'].eq(product)]
                imports=float(tf.loc[tf.direction.eq('import'),'amount'].sum());exports=float(tf.loc[tf.direction.eq('export'),'amount'].sum())
                po,uo=q('production',True),q('use',True);other_p,other_u=q('production',False),q('use',False);excluded=q('use',pricing=True)
                remainder=uo+other_u+exports-po-other_p-imports
                accounting=remainder+excluded
                tolerance=max(1e-6,1e-10*max(po+other_p+imports,uo+other_u+exports+excluded))
                energy.append(dict(scenario=scenario,year=year,product=product,unit='TJ/yr',own_production=po,own_use=uo,other_port_production=other_p,other_physical_port_use=other_u,imports=imports,exports=exports,excluded_pricing_use=excluded,own_net_requirement=uo-po,outside_port_supply=uo+other_u-po-other_p,physical_port_remainder=remainder,balance_with_pricing=accounting,physical_balance_closed=abs(remainder)<=tolerance,pricing_balance_closed=abs(accounting)<=tolerance,approved_for_market_use=False))
    return dict(data=combined,flow_details=detail,hydrogen_split=pd.DataFrame(split),energy_balance=pd.DataFrame(energy),row_directions=pd.DataFrame(directions),explicit_zeros=pd.DataFrame(zero))
