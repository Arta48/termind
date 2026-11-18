#!/bin/bash
set -e

if [[ "$(uname)" != "Linux" ]] || ! command -v pacman > /dev/null; then
    echo "This script can only be run on Arch-based Linux!"
    exit 1
fi

if [[ ! -d termind ]] || [[ ! -f pyproject.toml ]] || [[ ! -f LICENSE ]] || [[ ! -f README.md ]]; then
    echo "Error: Run this script from the root of the termind repository!"
    exit 1
fi

# Request sudo privileges upfront
sudo echo > /dev/null

# 1. Prepare clean staging directory
rm -rf termind_pkg
mkdir -p termind_pkg
tar --exclude='__pycache__' --exclude='*.py[cod]' -czf termind_pkg/termind-src.tar.gz termind pyproject.toml
cp README.md LICENSE termind_pkg/
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
    "termind-src.tar.gz"
    "README.md"
    "LICENSE"
)
sha256sums=(
    "SKIP"
    "SKIP"
    "SKIP"
)

prepare() {
    cd "${srcdir}"

    # Creating desktop entry file
    cat > "${pkgname}.desktop" <<_DESKTOP_EOF
#!/usr/bin/env xdg-open
[Desktop Entry]
Type=Application
Name=Termind
Comment=Cross-platform, multi-provider autonomous AI engineering assistant for your terminal
Exec=termind %U
Icon=utilities-terminal
Terminal=true
Categories=Development;Utility;ConsoleOnly;
StartupWMClass=${pkgname}
Keywords=ai;agent;terminal;cli;developer;assistant;gemini;claude;openai;deepseek;ollama;
MimeType=inode/directory;
_DESKTOP_EOF

    # Creating Dolphin Service Menu
    cat > "${pkgname}-servicemenu.desktop" <<_SERVICEMENU_EOF
[Desktop Entry]
Icon=utilities-terminal
Type=Service
Actions=OpenInTermind
ServiceTypes=KonqPopupMenu/Plugin
MimeType=inode/directory;
Terminal=true
StartupWMClass=${pkgname}

[Desktop Action OpenInTermind]
Name=Open with Termind
Name[ru]=Открыть в Termind
Icon=utilities-terminal
Exec=termind %U
_SERVICEMENU_EOF
}

package() {
    cd "${srcdir}"

    # Installing application files in /opt
    find termind -type f -name '*.py' -exec install -Dm644 {} "${pkgdir}/opt/${pkgname}/{}" \;
    install -Dm644 pyproject.toml "${pkgdir}/opt/${pkgname}/pyproject.toml"
    install -Dm644 README.md "${pkgdir}/opt/${pkgname}/README.md"

    # Installing MIT license
    install -Dm644 LICENSE "${pkgdir}/usr/share/licenses/${pkgname}/LICENSE"

    # Creating launcher executable in /usr/bin
    install -d "${pkgdir}/usr/bin"
    printf '#!/bin/sh\nexec python -P -c "import sys; sys.path.insert(0, '\''/opt/%s'\''); from termind.cli import main; main()" "$@"\n' "${pkgname}" > "${pkgdir}/usr/bin/${pkgname}"
    chmod 755 "${pkgdir}/usr/bin/${pkgname}"

    # Installing desktop file
    install -Dm644 "${pkgname}.desktop" "${pkgdir}/usr/share/applications/${pkgname}.desktop"

    # Installing Dolphin Service Menu
    install -Dm644 "${pkgname}-servicemenu.desktop" "${pkgdir}/usr/share/kio/servicemenus/${pkgname}.desktop"
}
EOF

# 3. Build and install package into the system
makepkg -si --skipinteg --noconfirm

cd .. && rm -rf termind_pkg

echo "
Done!
"
