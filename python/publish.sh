#!/bin/bash
# WS-Tunnel 发布脚本
#
# 使用方法：
#   ./publish.sh test    # 发布到 TestPyPI
#   ./publish.sh prod    # 发布到 PyPI
#   ./publish.sh build   # 仅构建，不发布

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

echo_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

echo_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 检查依赖
check_deps() {
    if ! command -v python &> /dev/null; then
        echo_error "Python not found"
        exit 1
    fi
    
    if ! python -c "import build" &> /dev/null; then
        echo_info "Installing build..."
        pip install build
    fi
    
    if ! python -c "import twine" &> /dev/null; then
        echo_info "Installing twine..."
        pip install twine
    fi
}

# 清理
clean() {
    echo_info "Cleaning build artifacts..."
    rm -rf dist/ build/ *.egg-info tunely.egg-info
}

# 构建
build() {
    echo_info "Building package..."
    python -m build
    
    echo_info "Checking package..."
    twine check dist/*
    
    echo_info "Build complete!"
    ls -la dist/
}

# 发布到 TestPyPI
publish_test() {
    echo_info "Publishing to TestPyPI..."
    twine upload --repository testpypi dist/*
    echo_info "Published to TestPyPI!"
    echo_info "Install with: pip install -i https://test.pypi.org/simple/ ws-tunnel"
}

# 发布到 PyPI
publish_prod() {
    echo_warn "Publishing to PyPI (production)..."
    read -p "Are you sure? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        twine upload dist/*
        echo_info "Published to PyPI!"
        echo_info "Install with: pip install ws-tunnel"
    else
        echo_info "Cancelled."
    fi
}

# 主函数
main() {
    check_deps
    clean
    
    case "${1:-build}" in
        build)
            build
            ;;
        test)
            build
            publish_test
            ;;
        prod)
            build
            publish_prod
            ;;
        *)
            echo "Usage: $0 {build|test|prod}"
            exit 1
            ;;
    esac
}

main "$@"
