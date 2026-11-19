"""Task-bound theme/state expectations; synthetic colours, no live app proof."""
import copy
import pytest
from plugins.fleet_lifecycle import navigation_preview as visual
from plugins.fleet_lifecycle.controller import Halt, Plan
from tests.plugins.test_fleet_navigation_preview import observations
from tests.plugins.test_fleet_project_verification import _runtime
from tests.plugins.test_fleet_project_visual_handoff import preview


def grey_observations():
    baseline, candidate = observations()
    expected = {
        'light': {'normal': [220,220,220], 'hover': [210,210,210],
                  'pressed': [200,200,200], 'keyboard-focus': [220,220,220]},
        'dark': {'normal': [20,20,20], 'hover': [30,30,30],
                 'pressed': [40,40,40], 'keyboard-focus': [20,20,20]},
    }
    for row in candidate['observations']:
        b = row['buttons'][0]
        b['backgroundRGB'] = copy.deepcopy(expected[row['scheme']][row['state']])
        b['background'] = 'rgb(' + ', '.join(map(str,b['backgroundRGB'])) + ')'
        b['textRGB'] = [0,0,0] if row['scheme'] == 'light' else [255,255,255]
        b['color'] = 'rgb(' + ', '.join(map(str,b['textRGB'])) + ')'
        if row['state'] == 'keyboard-focus':
            b['outline'] = b['color'] + ' solid 2px'
    return baseline, candidate, expected


def test_task_bound_grey_palette_accepts_each_theme_and_all_interactions_without_mutation():
    baseline,candidate,expected = grey_observations()
    original = copy.deepcopy((baseline,candidate,expected))
    checks = visual.validate_observations(baseline,candidate,expected)
    assert checks['states_checked'] == 8
    assert checks['minimum_label_contrast'] >= 4.5
    assert (baseline,candidate,expected) == original


@pytest.mark.parametrize('scheme',['light','dark'])
@pytest.mark.parametrize('state',['normal','hover','pressed','keyboard-focus'])
def test_task_bound_palette_rejects_wrong_colour_in_every_theme_state(scheme,state):
    baseline,candidate,expected = grey_observations()
    visual.validate_observations(baseline,candidate,expected)
    row = next(r for r in candidate['observations'] if (r['scheme'],r['state']) == (scheme,state))
    row['buttons'][0]['backgroundRGB'] = [128,128,128]
    with pytest.raises(visual.PreviewError,match='preview_target_color_mismatch'):
        visual.validate_observations(baseline,candidate,expected)


def test_frozen_plan_accepts_and_hashes_complete_task_theme_state_palette(tmp_path,monkeypatch):
    rt,data,_calls = _runtime(tmp_path,monkeypatch)
    spec = preview(rt,data,tmp_path)
    _b,_c,expected = grey_observations()
    spec['expected_background_rgb'] = copy.deepcopy(expected)
    before = copy.deepcopy(data)
    Plan.validate(data)
    assert data == before


@pytest.mark.parametrize('case',['missing_theme','extra_theme','missing_state','extra_state','boolean','overflow'])
def test_task_palette_rejects_incomplete_or_invalid_values(case):
    baseline,candidate,expected = grey_observations()
    if case == 'missing_theme': del expected['dark']
    elif case == 'extra_theme': expected['other'] = copy.deepcopy(expected['light'])
    elif case == 'missing_state': del expected['dark']['pressed']
    elif case == 'extra_state': expected['light']['other'] = [1,2,3]
    elif case == 'boolean': expected['dark']['hover'][0] = True
    else: expected['light']['normal'][1] = 256
    with pytest.raises(visual.PreviewError):
        visual.validate_observations(baseline,candidate,expected)


def test_existing_single_rgb_contract_keeps_original_behaviour():
    baseline,candidate = observations()
    assert visual.validate_observations(baseline,candidate,[236,72,153])['states_checked'] == 8


def test_explicit_state_contract_can_restore_identical_original_dark_hover_and_pressed():
    baseline,candidate,expected = grey_observations()
    expected['dark']['pressed'] = list(expected['dark']['hover'])
    pressed = next(r for r in candidate['observations'] if (r['scheme'],r['state']) == ('dark','pressed'))
    pressed['buttons'][0]['backgroundRGB'] = list(expected['dark']['pressed'])
    assert visual.validate_observations(baseline,candidate,expected)['states_checked'] == 8
