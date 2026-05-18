#!/bin/bash
# ════════════════════════════════════════════════════════════════
# auto_discuss 启动器 —— 双击运行,不用碰命令行
# auto_discuss 项目"收尾"阶段产出。canonical 版本随项目 git 管理:
#   ~/Developer/auto_discuss/启动讨论.command
# 桌面上那一份是供双击的副本;改动以项目里这份为准。
# ════════════════════════════════════════════════════════════════

# 双击运行的 .command 不会加载 shell 配置,这里补好 PATH 与 UTF-8 环境:
#  - PATH:让 python3 / claude / codex 能被找到
#  - PYTHONUTF8 / LANG:确保中文(含子进程输出)按 UTF-8 处理,不乱码
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:/usr/bin:/bin"
export PYTHONUTF8=1
export LANG="en_US.UTF-8"

SCRIPT="$HOME/Developer/auto_discuss/auto_discuss.py"
DISCUSS_DIR="$HOME/Developer/auto_discuss/讨论记录"

line() { echo "════════════════════════════════════════"; }
pause_exit() { echo ""; read -n1 -r -p "按任意键关闭本窗口。"; exit "${1:-0}"; }

echo ""
line
echo "          auto_discuss · 多方讨论"
line
echo ""

# —— 环境检查 ——
if [ ! -f "$SCRIPT" ]; then
  echo "✗ 找不到调度脚本:"
  echo "  $SCRIPT"
  echo "  请确认 auto_discuss 项目在 ~/Developer/auto_discuss/。"
  pause_exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "✗ 找不到 python3,无法运行。"
  pause_exit 1
fi
mkdir -p "$DISCUSS_DIR"

# —— 选择:新建 / 继续 ——
echo "想做什么?"
echo "  1) 新建一场讨论"
echo "  2) 继续一场已有讨论(例如上次没跑完)"
echo ""
read -r -p "输入 1 或 2 后回车:" CHOICE
echo ""

FILE=""
if [ "$CHOICE" = "1" ]; then
  # —— 新建:起名(仅文件名)→ 建骨架 → VS Code 打开让用户写问题 ——
  echo "第 1 步 · 给这场讨论起个名字。"
  echo "         (只用作文件名;你真正要讨论的【问题】下一步在 VS Code 里写。)"
  read -r -p "讨论名字(例:潮汐推送方案):" TITLE
  [ -z "$TITLE" ] && TITLE="讨论"
  SAFE=$(echo "$TITLE" | tr ' /:' '___')          # 去掉文件名里不能用的字符
  FILE="$DISCUSS_DIR/${SAFE}_$(date +%m%d).md"
  [ -e "$FILE" ] && FILE="$DISCUSS_DIR/${SAFE}_$(date +%m%d-%H%M).md"  # 同名不覆盖
  cat > "$FILE" <<EOF
# ${TITLE}讨论($(date +%Y-%m-%d))

## 话题 1

在下面写下你想讨论的问题 / 需求(写得越具体,讨论越到点),写完按 ⌘S 保存:


EOF
  echo ""
  echo "✓ 已新建讨论文件,并在 VS Code 打开:"
  echo "  $FILE"
  open -a "Visual Studio Code" "$FILE" 2>/dev/null || open "$FILE"
  echo ""
  echo "第 2 步 · 到刚打开的 VS Code 窗口,在「## 话题 1」下面写下你真正"
  echo "         想讨论的【问题 / 需求】,然后保存(⌘S)。"
  echo "第 3 步 · 回到屏幕上的弹窗,点【开始讨论】。"
  echo ""
  # 浮在最上层的原生弹窗代替"回终端按键"(终端窗口会被 VS Code 挡住,弹窗不会)
  DLG='display dialog "你要讨论的【问题】写在哪里?" & return & "→ 就在刚打开的那个 VS Code 文件里,「## 话题 1」标题下面。" & return & return & "在那里写好问题 / 需求并保存(⌘S),再点【开始讨论】。" & return & return & "(直接写问题就行,不用打 over。)" buttons {"取消", "开始讨论"} default button "开始讨论" cancel button "取消" with title "auto_discuss · 写好问题了吗"'
  if ! osascript -e "$DLG" >/dev/null 2>&1; then
    echo "已取消,没有开始讨论。讨论文件已建好,下次用「继续已有讨论」可打开:"
    echo "  $FILE"
    pause_exit 0
  fi
  echo ""
elif [ "$CHOICE" = "2" ]; then
  # —— 继续:列出已有讨论文件供选择 ——
  shopt -s nullglob
  FILES=()
  for f in "$DISCUSS_DIR"/*.md; do
    case "$f" in
      *.state.md | *.backup.*) ;;                 # 跳过白板文件与备份
      *) FILES+=("$f") ;;
    esac
  done
  if [ "${#FILES[@]}" -eq 0 ]; then
    echo "✗ $DISCUSS_DIR 里还没有讨论文件,先新建一场吧。"
    pause_exit 1
  fi
  echo "已有的讨论:"
  i=1
  for f in "${FILES[@]}"; do
    echo "  $i) $(basename "$f")"
    i=$((i + 1))
  done
  echo ""
  read -r -p "输入编号后回车:" NUM
  if ! echo "$NUM" | grep -qE '^[0-9]+$' || [ "$NUM" -lt 1 ] || [ "$NUM" -gt "${#FILES[@]}" ]; then
    echo "✗ 编号无效。"
    pause_exit 1
  fi
  FILE="${FILES[$((NUM - 1))]}"
  echo "✓ 继续讨论:$(basename "$FILE")"
  echo ""
else
  echo "✗ 只能输入 1 或 2。重新双击运行试试。"
  pause_exit 1
fi

# —— 启动调度器 ——
line
echo "讨论开始。Claude、Codex 会自动轮流发言。"
echo "轮到你时,程序会停下 —— 讨论文档末尾会冒出红色"
echo "的「🔴 用户回合」段;到 VS Code 里写你的意见,"
echo "另起一行打 over(继续)或 收工(结束),保存即可。"
line
echo ""

python3 "$SCRIPT" --file "$FILE"
RC=$?

echo ""
line
if [ "$RC" -eq 0 ]; then
  echo "✓ 讨论结束。完整记录在:"
else
  echo "讨论中断(退出码 $RC)。已写入的内容和日志都在:"
fi
echo "  $FILE"
line
pause_exit 0
