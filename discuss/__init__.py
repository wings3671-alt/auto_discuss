"""胡说八道群:让多个 AI 与用户围绕一个议题轮流发言的本机程序。

模块分工(依赖方向自上而下,上面的不认识下面的):

    config      设置与路径
    document    讨论文档(Markdown)的读与写
    whiteboard  状态白板(STATE.md)与 STATE_OPS 指令
    prompts     给 AI 的提示词
    providers   各家 AI 的调用方式(Claude / Codex / Ollama / 任意命令行)
    engine      调度:轮到谁、调用谁、写入什么
    server      本机网页服务(界面通过它读写)
"""

__version__ = "2.0.0"
