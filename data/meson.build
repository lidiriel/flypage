po_dir = meson.project_source_root() / 'po'

# Menu entry (settings) and the processing window's entry (hidden: it is
# opened by the daemon or by "flypage sort").
foreach name : ['io.github.flypage.Settings', 'io.github.flypage.Processing']
  i18n.merge_file(
    input: name + '.desktop.in',
    output: name + '.desktop',
    type: 'desktop',
    po_dir: po_dir,
    install: true,
    install_dir: get_option('datadir') / 'applications',
  )
endforeach

# Starts "flypage daemon" with every desktop session (XDG autostart).
daemon_desktop = i18n.merge_file(
  input: 'io.github.flypage.Daemon.desktop.in',
  output: 'io.github.flypage.Daemon.desktop',
  type: 'desktop',
  po_dir: po_dir,
  install: true,
  install_dir: get_option('sysconfdir') / 'xdg' / 'autostart',
)

metainfo = i18n.merge_file(
  input: 'io.github.flypage.metainfo.xml.in',
  output: 'io.github.flypage.metainfo.xml',
  po_dir: po_dir,
  install: true,
  install_dir: get_option('datadir') / 'metainfo',
)

install_subdir('icons/hicolor', install_dir: get_option('datadir') / 'icons')

desktop_validate = find_program('desktop-file-validate', required: false)
if desktop_validate.found()
  foreach name : ['io.github.flypage.Settings', 'io.github.flypage.Processing', 'io.github.flypage.Daemon']
    test('validate ' + name + '.desktop', desktop_validate,
         args: [meson.current_build_dir() / name + '.desktop'])
  endforeach
endif

appstreamcli = find_program('appstreamcli', required: false)
if appstreamcli.found()
  # No homepage yet: report it without failing the build.
  test('validate metainfo', appstreamcli,
       args: ['validate', '--no-net', '--explain', '--override=url-homepage-missing=info', metainfo])
endif

gnome = import('gnome')
gnome.post_install(gtk_update_icon_cache: true, update_desktop_database: true)
