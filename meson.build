project(
  'flypage',
  version: files('VERSION'),
  meson_version: '>= 1.1',
  license: 'GPL-3.0-or-later',
)

# The interpreter Meson itself runs under (/usr/bin/python3 on Debian),
# not whatever "python3" happens to be first in PATH.
python = import('python')
py = python.find_installation()

i18n = import('i18n')

prefix = get_option('prefix')
localedir = prefix / get_option('localedir')
pkgdatadir = prefix / get_option('datadir') / meson.project_name()
pythondir = py.get_install_dir()

subdir('flypage')
subdir('bin')
subdir('data')
subdir('po')
subdir('tests')
