if run_command(py, '-c', 'import pytest, tomli_w', check: false).returncode() == 0
  test(
    'pytest',
    py,
    args: ['-m', 'pytest', '-q', meson.project_source_root() / 'tests'],
    workdir: meson.project_source_root(),
    timeout: 600,  # OCR and window tests; well under a minute normally
  )
else
  warning('pytest or tomli_w not found for @0@: tests disabled'.format(py.full_path()))
endif
