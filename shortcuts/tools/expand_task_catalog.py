"""将完整的查询→领取→确认动作块扩展到任务 1–13；重复执行不重复新增。"""
import copy
import json
from pathlib import Path
import uuid

SOURCE = Path(__file__).resolve().parents[1] / 'source/workflow.json'

def expand(workflow):
    actions = workflow['WFWorkflowActions']
    start = next(i for i, a in enumerate(actions)
                 if a['WFWorkflowActionParameters'].get('WFConditionalActionString') == 'claim')
    group = actions[start]['WFWorkflowActionParameters']['GroupingIdentifier']
    end = next(i for i in range(start + 1, len(actions))
               if actions[i]['WFWorkflowActionParameters'].get('GroupingIdentifier') == group)
    # 每个任务块以读取配置 token 开始；仅取 claim 条件直接子层，排除块内的再次查询。
    depth = 0
    starts = []
    for i in range(start + 1, end):
        p = actions[i]['WFWorkflowActionParameters']
        if depth == 0 and p.get('WFDictionaryKey') == 'form.token':
            starts.append(i)
        if p.get('WFControlFlowMode') == 0:
            depth += 1
        elif p.get('WFControlFlowMode') == 2:
            depth -= 1
    assert depth == 0 and starts[0] == start + 1
    blocks = {}
    for left, right in zip(starts, starts[1:] + [end]):
        block = actions[left:right]
        claims = [a['WFWorkflowActionParameters'] for a in block
                  if isinstance(a['WFWorkflowActionParameters'].get('WFURL'), str) and a['WFWorkflowActionParameters']['WFURL'].endswith('/partnergettaskreward2611')]
        assert len(claims) == 1
        form = claims[0]['WFFormValues']['Value']['WFDictionaryFieldValueItems']
        task_id = int(next(f['WFValue']['Value']['string'] for f in form
                           if f['WFKey']['Value']['string'] == 'task_id'))
        blocks[task_id] = block
    template = blocks[5]
    def clone(task_id):
        ids = {}
        for a in template:
            for key in ('UUID', 'GroupingIdentifier'):
                value = a['WFWorkflowActionParameters'].get(key)
                if value:
                    ids.setdefault(value, str(uuid.uuid4()).upper())
        def remap(value):
            if isinstance(value, dict):
                return {k: remap(v) for k, v in value.items()}
            if isinstance(value, list):
                return [remap(v) for v in value]
            return ids.get(value, value) if isinstance(value, str) else value
        block = remap(copy.deepcopy(template))
        for a in block:
            p = a['WFWorkflowActionParameters']
            if p.get('WFNumberValue') == 5:
                p['WFNumberValue'] = task_id
            if isinstance(p.get('WFURL'), str) and p['WFURL'].endswith('/partnergettaskreward2611'):
                for field in p['WFFormValues']['Value']['WFDictionaryFieldValueItems']:
                    if field['WFKey']['Value']['string'] == 'task_id':
                        field['WFValue']['Value']['string'] = str(task_id)
            if p.get('WFTextActionText') == '任务 5：领取已确认。':
                p['WFTextActionText'] = f'任务 {task_id}：领取已确认。'
        return block
    actions[start + 1:end] = [a for task_id in range(1, 14)
                              for a in (blocks[task_id] if task_id in blocks else clone(task_id))]
    for a in actions:
        p = a['WFWorkflowActionParameters']
        if p.get('WFMatchTextPattern') == '^(?:2|3|4|5|12|13)$':
            p['WFMatchTextPattern'] = '^(?:[1-9]|1[0-3])$'
        if 'WFCommentActionText' in p:
            p['WFCommentActionText'] = p['WFCommentActionText'].replace(
                '当前 2611 手机白名单 2/3/4/5/12/13，领取 type=1。服务器/Windows 还含 ID 1，历史差异见总说明；新增接口需同步三端。',
                '2611 手机白名单任务 1–13，领取 type=1。ID 6–11 为根据任务列表及已知接口推断的参数，尚无成功抓包佐证；仅在最新状态为可领取时提交。各端同步维护。')
            p['WFCommentActionText'] = p['WFCommentActionText'].replace('2611 活动仅领 2/3/4/5/12/13。', '2611 活动仅领任务 1–13（6–11 参数为推断）。')
    workflow['WFWorkflowName'] = 'LostArkHelper-iPhone-v0.2.0'
    return workflow

if __name__ == '__main__':
    workflow = expand(json.loads(SOURCE.read_text(encoding='utf-8')))
    SOURCE.write_text(json.dumps(workflow, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('Updated iPhone catalog: tasks 1–13 (6–11 inferred).')
