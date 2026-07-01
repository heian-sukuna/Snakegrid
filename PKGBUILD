# Maintainer: Ryan Wanyika (malvryn) <ryanwanyika@gmail.com>
pkgname=snakegrid-git
_pkgname=snakegrid
pkgver=r7.cf67e06
pkgrel=1
pkgdesc="A tiny, dependency-free snake-grid layout daemon for Hyprland"
arch=('any')
url="https://github.com/heian-sukuna/Snakegrid"
license=('MIT')
depends=('python' 'hyprland')
optdepends=('libnotify: desktop notifications on toggle')
makedepends=('git')
provides=('snakegrid')
conflicts=('snakegrid')
source=("$_pkgname::git+https://github.com/heian-sukuna/Snakegrid.git")
sha256sums=('SKIP')

pkgver() {
  cd "$srcdir/$_pkgname"
  printf "r%s.%s" "$(git rev-list --count HEAD)" "$(git rev-parse --short HEAD)"
}

package() {
  cd "$srcdir/$_pkgname"
  # daemon lives in a system path; the `snakegrid` command finds it there
  install -Dm755 snake-grid.py "$pkgdir/usr/lib/snakegrid/snake-grid.py"
  install -Dm755 snakegrid     "$pkgdir/usr/bin/snakegrid"
  install -Dm644 README.md     "$pkgdir/usr/share/doc/$_pkgname/README.md"
  install -Dm644 LICENSE       "$pkgdir/usr/share/licenses/$_pkgname/LICENSE"
}
