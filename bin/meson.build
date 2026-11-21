launcher_conf = configuration_data()
launcher_conf.set('PYTHON', py.full_path())
launcher_conf.set('PYTHONDIR', pythondir)

configure_file(
  input: 'flypage.in',
  output: 'flypage',
  configuration: launcher_conf,
  install: true,
  install_dir: get_option('bindir'),
  install_mode: 'rwxr-xr-x',
)
