#Maintainer: Kent Hou Man <knthmn0@gmail.com>

pkgname=pacbundle
pkgver=0.1.0
pkgrel=1
pkgdesc="A declarative pacman wrapper for Arch Linux"
arch=('any')
url="https://github.com/knthmn/pacbundle"
license=('MIT')
depends=('python-pydantic' 'python-typer' 'python-rich')
source=("$pkgname-$pkgver.tar.gz::$url/archive/refs/tags/v$pkgver.tar.gz")
sha256sums=('2dde2dcc227e4fac8bdcafd15affb7e9bae26f87393dbe52531587fbbbbed4cc')

package() {
	install -Dm775 "$srcdir/$pkgname-$pkgver/pacbundle/main.py" "$pkgdir/usr/bin/pacbundle"
}
