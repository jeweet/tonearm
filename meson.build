project('tonearm',
  version: '0.4.0',
  meson_version: '>= 0.62.0',
  license: 'MIT',
)

app_id = 'io.github.jeweet.Tonearm'
python = import('python')
py = python.find_installation('python3')
gnome = import('gnome')

pkgdatadir = get_option('prefix') / get_option('datadir') / meson.project_name()

install_subdir('tonearm',
  install_dir: pkgdatadir,
  exclude_directories: ['__pycache__', 'ui/__pycache__'],
)

conf = configuration_data()
conf.set('PYTHON', py.full_path())
conf.set('PKGDATADIR', pkgdatadir)
configure_file(
  input: 'tonearm.in',
  output: 'tonearm',
  configuration: conf,
  install: true,
  install_dir: get_option('bindir'),
  install_mode: 'rwxr-xr-x',
)

install_data('data' / app_id + '.desktop', install_dir: get_option('datadir') / 'applications')
install_data('data' / app_id + '.metainfo.xml', install_dir: get_option('datadir') / 'metainfo')
install_data('data' / 'icons' / app_id + '.svg',
  install_dir: get_option('datadir') / 'icons' / 'hicolor' / 'scalable' / 'apps')
install_data('data' / 'icons' / app_id + '-symbolic.svg',
  install_dir: get_option('datadir') / 'icons' / 'hicolor' / 'symbolic' / 'apps')

# Checks for the desktop entry and the AppStream data (when the tools are installed)
desktop_validate = find_program('desktop-file-validate', required: false)
if desktop_validate.found()
  test('desktop-file', desktop_validate, args: [meson.project_source_root() / 'data' / app_id + '.desktop'])
endif
appstreamcli = find_program('appstreamcli', required: false)
if appstreamcli.found()
  test('metainfo', appstreamcli,
    args: ['validate', '--no-net', '--explain',
           meson.project_source_root() / 'data' / app_id + '.metainfo.xml'])
endif

gnome.post_install(gtk_update_icon_cache: true, update_desktop_database: true)
