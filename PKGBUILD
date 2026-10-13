# Maintainer: tak0dan
pkgname=uni-pkg-git
pkgver=r3.764a3ee
pkgrel=1
pkgdesc="Unified wrapper for native package managers, with a name resolver and source hierarchy"
arch=('any')
url="https://github.com/tak0dan/UNI-pkg-manager"
depends=('python' 'python-rapidfuzz' 'python-textual' 'python-rich')
makedepends=('python-build' 'python-installer' 'python-setuptools' 'python-wheel')
provides=('uni-pkg')
conflicts=('uni-pkg')
source=("$pkgname::git+$url")
sha256sums=('SKIP')

pkgver() {
  cd "$pkgname"
  printf 'r%s.%s' "$(git rev-list --count HEAD)" "$(git rev-parse --short HEAD)"
}

build() {
  cd "$pkgname"
  python -m build --wheel --no-isolation
}

package() {
  cd "$pkgname"
  python -m installer --destdir="$pkgdir" dist/*.whl
  install -Dm644 README.md "$pkgdir/usr/share/doc/$pkgname/README.md"
  install -Dm644 UNI_DESIGN.md "$pkgdir/usr/share/doc/$pkgname/UNI_DESIGN.md"
}
