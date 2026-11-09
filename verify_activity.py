"""核对四种运行方式的活动版本、支持任务与推断标记。"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
manifest = json.loads((ROOT/'activity-manifest.json').read_text())
for name in ('server', 'windows', 'linux'):
    cfg = json.loads((ROOT/name/'service/activity.json').read_text())
    assert cfg['version'] == manifest['version'], name
    assert sorted(map(int, cfg['verified_claims'])) == manifest[name], name
    assert all(type(v) is int and v == 1 for v in cfg['verified_claims'].values()), name
    assert sorted(cfg['inferred_task_ids']) == manifest['inferred_task_ids'], name
workflow = json.loads((ROOT/'shortcuts/source/workflow.json').read_text())
claims = []
for action in workflow['WFWorkflowActions']:
    p = action['WFWorkflowActionParameters']
    if action['WFWorkflowActionIdentifier'].endswith('.downloadurl') and isinstance(p.get('WFURL'), str) and 'partnergettaskreward' in p['WFURL']:
        assert p['WFURL'].endswith(manifest['version'])
        values = {x['WFKey']['Value']['string']: x['WFValue']['Value']['string']
                  for x in p['WFFormValues']['Value']['WFDictionaryFieldValueItems']
                  if x['WFKey']['Value']['string'] in ('task_id', 'type')}
        assert values['type'] == '1'
        claims.append(int(values['task_id']))
assert sorted(claims) == manifest['shortcuts']
print('服务器、Windows、Linux、iPhone 活动配置一致；任务1–13，6–11保留推断标记。')
