#!/bin/bash
# ============================================================
#  VOID COLOSSEUM — 一键部署脚本
#  在你的电脑上运行这一个文件，全部自动完成
# ============================================================

set -e
echo ""
echo "🏟  VOID COLOSSEUM — 一键部署"
echo "================================"
echo ""

# Step 1: 创建项目目录
echo "📁 创建项目..."
mkdir -p void-colosseum/public
cd void-colosseum

# Step 2: 下载游戏文件（如果本地有tar包就跳过）
if [ ! -f "public/index.html" ]; then
    echo "⚠  请先把 void_colosseum_v02.html 复制到 void-colosseum/public/index.html"
    echo "   或者把下载的 tar.gz 解压到当前目录"
    exit 1
fi

# Step 3: 初始化 Git
echo "🔧 初始化 Git..."
git init -q
cat > .gitignore << 'EOF'
node_modules/
.vercel/
__pycache__/
*.pyc
EOF
git add .
git commit -q -m "🏟 VOID COLOSSEUM V0.2 — Agent Arena"

# Step 4: 创建 GitHub 仓库并推送
echo "🚀 推送到 GitHub..."
if command -v gh &> /dev/null; then
    gh repo create void-colosseum --public --source=. --push -y
    echo "✅ GitHub 仓库已创建"
else
    echo "⚠  未安装 gh CLI，请手动:"
    echo "   1. 去 github.com/new 创建 void-colosseum 仓库"
    echo "   2. git remote add origin https://github.com/你的用户名/void-colosseum.git"
    echo "   3. git push -u origin main"
fi

# Step 5: 部署到 Vercel
echo ""
echo "🌐 部署到 Vercel..."
if command -v vercel &> /dev/null || command -v npx &> /dev/null; then
    npx vercel --prod --yes 2>/dev/null || vercel --prod --yes
    echo ""
    echo "============================================"
    echo "🎉 部署完成！"
    echo "============================================"
    echo ""
    echo "把 Vercel 给你的 URL 发回给 Claude"
    echo "我们立刻开始 Phase 2！"
else
    echo "⚠  请先安装 Node.js: https://nodejs.org"
    echo "   然后重新运行此脚本"
fi
