"""给 AI 的提示词。只拼文字,不读写任何文件。"""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_RULES = """\
## 讨论守则(每轮必读)
1. 不要无条件附和其他参与者。对他们的观点做批判性审视,明确指出漏洞、风险、未被验证的假设。
2. 拿出发散性思维:在别人方案之外,主动提出至少一个不同思路或反例。
3. 讨论既要有深度(往下深挖细节),也要有广度(往外拓展被忽略的方面)。
4. 宁可暴露分歧,也不要为了表面和气而含糊带过。
"""

OPS_GUIDE = """\
STATE_OPS 代码块 —— 告诉程序如何更新状态白板(程序会机械执行):
- 写成一个围栏代码块,信息串写 STATE_OPS,块内容是一个合法 JSON 对象。
- 形如:  {"ops": [ ...操作... ]}    ops 是数组;本轮无需改动就写 {"ops": []}
- 每条操作是下面三种之一:
    新增一条:  {"op": "add", "section": "<栏目>", "text": "<内容>"}
    修改一条:  {"op": "update", "id": "<编号>", "text": "<新内容>"}
    删除一条:  {"op": "remove", "id": "<编号>"}
- <栏目> 四选一:conclusions(已达成的结论) / open_questions(待解决的问题)
                  / rejected(已否决的方案) / constraints(关键约束 / 前提)
- <编号> 必须是白板上【已存在】的(如 C1、Q2、R1、K3)。新增条目不要自己写
  编号,程序会分配。每个编号在同一批里最多引用一次。
- rejected 条目的 text 要同时写清"方案"和"否决原因"。
- "某问题解决了" = 一条 remove 删掉那个 Q + 一条 add 加一条新结论。
- 不要写规定之外的字段。每轮都必须输出 STATE_OPS 块。
示例:
```STATE_OPS
{"ops": [
  {"op": "remove", "id": "Q3"},
  {"op": "add", "section": "conclusions", "text": "推送时间按当天潮汐动态计算"}
]}
```"""


@dataclass
class TurnContext:
    """一个 AI 发言回合需要知道的全部前情。"""

    speaker: str                              # 当前发言者的显示名
    round: int
    topic: str
    rules: str
    board: str                                # 白板全文
    doc_path: str                             # 完整讨论记录的路径,供回查
    others: list[tuple[str, str]] = field(default_factory=list)   # (名字, 最近一次发言)
    user_name: str = "用户"
    user_text: str = ""                       # 用户最近一次发言
    image_paths: list[str] = field(default_factory=list)
    can_see_images: bool = True


def turn(ctx: TurnContext) -> str:
    parts = [
        f"你正在参与一场多方讨论。当前轮到你(**{ctx.speaker}**)发言,这是第 {ctx.round} 轮。",
        "",
        "【讨论守则 —— 每轮必读】",
        ctx.rules.strip(),
        "",
        "【议题】",
        ctx.topic.strip() or "(议题为空,请先请用户说明想讨论什么)",
        "",
        "【当前状态白板】",
        "这是到目前为止讨论的结构化汇总,是你了解前情的【主要依据】:",
        "————",
        ctx.board.strip(),
        "————",
        "",
    ]
    for name, text in ctx.others:
        parts += [f"【{name} 最近一次发言】", text.strip(), ""]
    if not ctx.others:
        parts += ["【其他 AI 成员尚未发言】", ""]
    if ctx.user_text.strip():
        parts += [
            f"【{ctx.user_name}(项目决策者)最近一次发言 —— 本轮必须执行项】",
            f"{ctx.user_name} 是本项目决策者,不是普通的第三方意见。下面是原文(全文,不得"
            "压缩),你必须逐条照办、不得敷衍;若要求查网页或资料,你必须真的联网去查,"
            "不许凭印象作答:",
            "——原文——",
            ctx.user_text.strip(),
            "——原文结束——",
            "",
        ]
    if ctx.image_paths:
        parts += _image_lines(ctx)
    parts += [
        "【你这一轮要做的】",
        f"1. 基于白板和上面的最新发言,发表你第 {ctx.round} 轮的意见。遵守讨论守则:"
        "批判性审视、指出漏洞与风险、提发散思路,不要无条件附和。",
        f"2. 若白板上某条信息不够、需要看原文:完整讨论记录在文件 {ctx.doc_path},"
        "按『第 X 轮』分段,你可以读它回查【特定轮次】——但不要整篇通读(又慢又贵)。"
        "平时靠白板即可。",
        "3. 【重要】你不要编辑或写入任何文件。把发言直接作为回复【输出成文字】,"
        "程序会替你写进讨论记录。",
        "4. 引用网页时给出完整网址(https:// 开头),界面会把它显示成可点击的链接。",
        "5. 在发言正文之后、另起一行,输出一个 STATE_OPS 代码块,告诉程序如何把这一轮"
        "的新进展更新进白板。",
        "",
        OPS_GUIDE,
        "",
        "【输出格式】先输出发言正文(Markdown,不要写小节标题,程序会加),"
        "紧接着输出 STATE_OPS 代码块。不要任何额外前言或解释。",
    ]
    return "\n".join(parts)


def _image_lines(ctx: TurnContext) -> list[str]:
    if not ctx.can_see_images:
        return [f"【附图】{ctx.user_name} 附了 {len(ctx.image_paths)} 张图片,但你无法查看"
                "图片;如果图片内容对判断很重要,请明确说明你没看到。", ""]
    return [f"【附图】{ctx.user_name} 附了下面的图片作为佐证。请先查看图片再作答"
            "(若图片已随消息附上则直接看,否则用读文件工具打开):",
            *[f"- {path}" for path in ctx.image_paths], ""]


def retry(base: str, problem: str) -> str:
    return (f"{base}\n\n【重要纠正】你上次回复的 STATE_OPS 有问题:{problem}。"
            "请重新完整作答(发言正文 + STATE_OPS 代码块),务必让 STATE_OPS 是合法 JSON"
            "且符合上面说明的格式。")


def tidy(board: str) -> str:
    """请 AI 整理白板:合并重复、删去过时,不发表新观点。"""
    return "\n".join([
        "下面是一场讨论的状态白板,它越写越长了。请你整理它,让它更短、更清楚,"
        "但不丢失任何仍然有效的信息:",
        "- 合并重复或高度重叠的条目(update 其中一条,remove 其余)。",
        "- 删除已经过时、已被后续结论取代的条目。",
        "- 把啰嗦的条目改写得更简练(update)。",
        "- 不要新增观点,不要改变任何结论的含义。",
        "",
        "【当前状态白板】",
        "————",
        board.strip(),
        "————",
        "",
        "【重要】你不要编辑或写入任何文件。",
        "",
        OPS_GUIDE,
        "",
        "【输出格式】先用一两句话说明你整理了什么,紧接着输出 STATE_OPS 代码块。",
    ])
