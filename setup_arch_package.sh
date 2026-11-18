#!/bin/bash
set -e

if [[ "$(uname)" != "Linux" ]] || ! command -v pacman > /dev/null; then
    echo "This script can only be run on Arch-based Linux!"
    exit 1
fi

if [[ ! -f termind.py ]] || [[ ! -f LICENSE ]] || [[ ! -f README.md ]]; then
    echo "Error: Run this script from the root of the termind repository!"
    exit 1
fi

# Request sudo privileges upfront
sudo echo > /dev/null

# 1. Prepare clean staging directory
rm -rf termind_pkg
mkdir -p termind_pkg
cp termind.py README.md LICENSE termind_pkg/
cd termind_pkg

export PACKAGER="Arta <arta@gmail.com>"

# 2. Generate PKGBUILD
cat > PKGBUILD << 'EOF'
# Maintainer: Arta <arta@gmail.com>
pkgname=termind
pkgver=1.0.0
pkgrel=1
pkgdesc="Cross-platform, multi-provider autonomous AI engineering assistant for your terminal"
arch=("any")
url="https://github.com/Arta48/termind"
license=("MIT")
depends=(
    "python>=3.11"
    "python-google-genai"
    "python-openai"
    "python-anthropic"
    "python-rich"
    "python-mcp"
)
optdepends=(
    "ollama: run local LLM models offline"
    "nodejs: run Node.js-based MCP servers via npx"
    "uv: run Python MCP servers via uvx"
)
source=(
    "termind.py"
    "README.md"
    "LICENSE"
)
sha256sums=(
    "SKIP"
    "SKIP"
    "SKIP"
)

package() {
    cd "${srcdir}"

    # Installing application files in /opt
    install -Dm755 termind.py "${pkgdir}/opt/${pkgname}/termind.py"
    install -Dm644 README.md "${pkgdir}/opt/${pkgname}/README.md"

    # Installing MIT license
    install -Dm644 LICENSE "${pkgdir}/usr/share/licenses/${pkgname}/LICENSE"

    # Creating launcher executable in /usr/bin
    install -d "${pkgdir}/usr/bin"
    printf '#!/bin/sh\nexec python /opt/%s/termind.py "$@"\n' "${pkgname}" > "${pkgdir}/usr/bin/${pkgname}"
    chmod 755 "${pkgdir}/usr/bin/${pkgname}"
}
EOF

# 3. Build and install package into the system
makepkg -si --skipinteg --noconfirm

cd .. && rm -rf termind_pkg

echo "
Done!
"
