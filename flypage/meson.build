build_conf = configuration_data()
build_conf.set_quoted('VERSION', meson.project_version())
build_conf.set_quoted('LOCALEDIR', localedir)
build_conf.set_quoted('PKGDATADIR', pkgdatadir)

configure_file(
  input: '_build.py.in',
  output: '_build.py',
  configuration: build_conf,
  install: true,
  install_dir: pythondir / 'flypage',
)

py.install_sources(
  files(
    '__init__.py',
    '__main__.py',
    'application.py',
    'cancel.py',
    'cli.py',
    'config.py',
    'daemon.py',
    'defs.py',
    'device.py',
    'i18n.py',
    'importer.py',
    'languages.py',
    'model.py',
    'naming.py',
    'notifications.py',
    'paths.py',
    'pending.py',
    'pipeline.py',
    'progress.py',
    'scanners.py',
    'session.py',
    'thumbnails.py',
  ),
  subdir: 'flypage',
)

py.install_sources(
  files(
    'ocr/__init__.py',
    'ocr/base.py',
    'ocr/tesseract.py',
  ),
  subdir: 'flypage/ocr',
)

py.install_sources(
  files(
    'ui/__init__.py',
    'ui/activation.py',
    'ui/destination_bar.py',
    'ui/dialogs.py',
    'ui/processing_window.py',
    'ui/settings_window.py',
    'ui/style.css',
    'ui/template_editor.py',
    'ui/thumbnail_grid.py',
    'ui/widgets.py',
  ),
  subdir: 'flypage/ui',
)

py.install_sources(
  files('ui/icons/drag-document.svg'),
  subdir: 'flypage/ui/icons',
)

# Fallbacks for icon themes that lack them (see application._setup_icons).
py.install_sources(
  files(
    'ui/icons/hicolor/symbolic/apps/document-edit-symbolic.svg',
    'ui/icons/hicolor/symbolic/apps/image-missing-symbolic.svg',
  ),
  subdir: 'flypage/ui/icons/hicolor/symbolic/apps',
)
