"""为现有 iPhone 动作图添加 QQ 身份校验；在保存链接和游戏请求之前执行。"""
import contextlib
import json
from pathlib import Path
import uuid

SOURCE = Path(__file__).resolve().parents[1] / 'source/workflow.json'
MARKER = 'QQ OAuth 登录有效性校验 v1'


def attachment(value):
    return {'WFSerializationType': 'WFTextTokenAttachment', 'Value': value}


def variable(name):
    return attachment({'Type': 'Variable', 'VariableName': name})


def interpolate(*parts):
    text, tokens = '', {}
    for part in parts:
        if isinstance(part, dict):
            tokens['{%d, 1}' % (len(text.encode('utf-16-le')) // 2)] = part['Value']
            text += '\ufffc'
        else:
            text += part
    return {'WFSerializationType': 'WFTextTokenString', 'Value': {'string': text, 'attachmentsByRange': tokens}}


class Actions:
    def __init__(self): self.actions = []

    def add(self, kind, **params):
        uid = str(uuid.uuid4()).upper()
        params['UUID'] = uid
        self.actions.append({'WFWorkflowActionIdentifier': 'is.workflow.actions.' + kind,
                             'WFWorkflowActionParameters': params})
        return attachment({'Type': 'ActionOutput', 'OutputUUID': uid, 'OutputName': '结果'})

    def text(self, value): return self.add('gettext', WFTextActionText=interpolate(value))
    def get(self, value, key):
        return self.add('getvalueforkey', WFInput=value, WFDictionaryKey=key, WFGetDictionaryValueType='Value')

    def match(self, value, pattern):
        return self.add('text.match', text=interpolate(value), WFMatchTextPattern=pattern, WFMatchTextCaseSensitive=True)

    def count(self, value):
        count = self.add('count', Input=value, WFCountType='Items')
        return self.add('number', WFNumberActionNumber=count)

    @contextlib.contextmanager
    def conditional(self, value, expected):
        group = str(uuid.uuid4()).upper()
        field = 'WFNumberValue' if isinstance(expected, int) else 'WFConditionalActionString'
        self.add('conditional', WFInput={'Type': 'Variable', 'Variable': value}, WFCondition=5,
                 GroupingIdentifier=group, WFControlFlowMode=0, **{field: expected})
        yield
        self.add('conditional', GroupingIdentifier=group, WFControlFlowMode=2)

    def require(self, value, expected, message):
        with self.conditional(value, expected):
            result = self.text(message)
            self.add('showresult', Text=interpolate(result))
            self.add('exit', WFResult=result)


def update(workflow):
    actions = workflow['WFWorkflowActions']
    if any(a['WFWorkflowActionParameters'].get('WFCommentActionText') == MARKER for a in actions):
        return workflow
    b = Actions()
    b.add('comment', WFCommentActionText=MARKER)
    access = b.get(variable('配置'), 'form.access_token')
    # 链接解析已经要求 token 仅含 URL 安全字符；凭据只发送给 QQ 官方固定 HTTPS 地址。
    result = b.add('downloadurl', WFURL=interpolate('https://graph.qq.com/oauth2.0/me?access_token=', access, '&fmt=json'),
                   WFHTTPMethod='GET', Advanced=True)
    raw = b.text(result)
    # 兼容 QQ 的 JSON 与 callback(JSON) 返回，先提取唯一 JSON 对象再解析。
    matches = b.match(raw, r'(?s)\{.*\}')
    b.require(b.count(matches), 1, 'QQ 登录校验返回格式异常，本次停止。请稍后重试。')
    data = b.add('detect.dictionary', WFInput=b.text(matches))
    error = b.text(b.get(data, 'error'))
    b.require(b.count(b.match(error, '.+')), 0,
              'QQ 登录凭据已过期或失效。请删除 iCloud Drive/Shortcuts/lostark-local-link.txt，重新打开官方 App 获取新的完整活动链接后再运行。本次未签到或领奖。')
    for key in ('client_id', 'openid'):
        value = b.text(b.get(data, key))
        b.require(b.count(b.match(value, r'^[A-Za-z0-9_-]+$')), 1, 'QQ 登录校验返回格式异常，本次停止。请稍后重试。')
        expected = b.get(variable('配置'), 'form.' + ('appid' if key == 'client_id' else 'openid'))
        b.require(value, interpolate(expected),
                  'QQ 登录凭据与链接中的应用或账号不一致。请删除 iCloud Drive/Shortcuts/lostark-local-link.txt，重新获取完整链接。')
    position = next(i for i, a in enumerate(actions)
                    if a['WFWorkflowActionParameters'].get('WFVariableName') == '配置') + 1
    actions[position:position] = b.actions
    return workflow


if __name__ == '__main__':
    workflow = update(json.loads(SOURCE.read_text(encoding='utf-8')))
    SOURCE.write_text(json.dumps(workflow, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('iPhone QQ login validation inserted before saving and game API requests.')
