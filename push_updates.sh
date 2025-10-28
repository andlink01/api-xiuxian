#!/bin/bash
# 这是一个更健壮的 Git 自动推送脚本，增加了变更文件清单显示

# --- 配置 ---
# set -e: 脚本中任何命令返回非零退出码时立即退出。
# set -o pipefail: 管道中的任何命令失败，整个管道都算失败。
set -e
set -o pipefail

# --- 默认提交信息 ---
# 当您不提供参数时使用此信息
DEFAULT_MESSAGE="feat: 自动同步更新"

# --- 脚本开始 ---

# 1. 获取用户提供的提交信息 (第一个参数)
#    语法解释: ${1:-$DEFAULT_MESSAGE}
#    如果 $1 (第一个参数) 存在且不为空, 则使用 $1, 否则使用 $DEFAULT_MESSAGE
COMMIT_MESSAGE="${1:-$DEFAULT_MESSAGE}"

echo "-----------------------------------"
echo "🚀 启动自动推送脚本..."
echo "-----------------------------------"

# 2. 检查是否在 Git 仓库中
if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "❌ 错误：当前目录不是一个 Git 仓库。"
    exit 1
fi

# 3. (关键) 先从远程拉取更新 (pull)
#    这是防止 "push" 失败的最重要步骤
echo "🔄 [1/5] 正在从 GitHub (origin/main) 拉取最新变更..."
if ! git pull origin main; then
    echo "❌ 错误：'git pull' 失败。"
    echo "    这通常意味着您有本地变更与远程变更冲突了 (Merge Conflict)。"
    echo "    请在您的终端中手动解决冲突。"
    echo "    (解决后, 先不要运行此脚本, 而是手动 'git commit'，然后再 'git push')"
    exit 1
fi
echo "✅ 拉取成功。"

# 4. 添加所有本地变更到暂存区
echo "➕ [2/5] 正在添加所有本地文件变更 (git add .)..."
git add .

# 5. (新增) 显示暂存区的文件变更状态
echo "🔍 [3/5] 检测到的文件变更:"
# 使用 git status --short 来显示简短的状态信息
# M = Modified (修改), A = Added (新增), D = Deleted (删除), ?? = Untracked (未跟踪)
# --untracked-files=no 选项可以忽略未跟踪的文件，如果需要显示它们，可以去掉这个选项或改为 all
if git status --short --untracked-files=no | grep -q '.'; then
    git status --short --untracked-files=no | while IFS= read -r line; do
        status=$(echo "$line" | awk '{print $1}')
        file=$(echo "$line" | awk '{$1=""; print $0}' | sed 's/^[[:space:]]*//') # 提取文件名
        case "$status" in
            M) echo "    🔄 修改: $file" ;;
            A) echo "    ✨ 新增: $file" ;;
            D) echo "    🗑️ 删除: $file" ;;
            R) echo "    ➡️ 重命名: $file" ;; # R status通常表示重命名
            C) echo "    ©️ 复制: $file" ;;   # C status通常表示复制
            *) echo "    ❓ 其他 ($status): $file" ;; # 处理其他可能的状态
        esac
    done
else
    echo "    ✅ 没有检测到新的文件变更需要提交。"
    echo "✅ 本地已与远程同步。无需操作。"
    echo "-----------------------------------"
    exit 0
fi

# 6. 提交变更 (原步骤 5)
echo "📝 [4/5] 正在提交本地变更..."
echo "    提交日志: \"$COMMIT_MESSAGE\""
if ! git commit -m "$COMMIT_MESSAGE"; then
    echo "❌ 错误：'git commit' 失败。"
    echo "    请手动运行 'git status' 检查问题。"
    exit 1
fi

# 7. 推送到远程仓库 (原步骤 6)
echo "📡 [5/5] 正在推送到 GitHub (origin/main)..."
if ! git push origin main; then
    echo "❌ 错误：'git push' 失败。"
    echo "    这不应该发生，因为我们刚刚才 'pull' 过。"
    echo "    请检查您的网络连接或 GitHub 状态。"
    exit 1
fi

echo "-----------------------------------"
echo "✅ 成功！所有变更已同步到 GitHub。"
echo "-----------------------------------"
