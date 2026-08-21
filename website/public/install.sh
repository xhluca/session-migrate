#!/bin/sh
set -eu

package="${SESSION_MIGRATE_PACKAGE:-session-migrate}"
if [ -n "${SESSION_MIGRATE_VERSION:-}" ] && [ "$package" = "session-migrate" ]; then
  package="session-migrate==${SESSION_MIGRATE_VERSION}"
fi

if command -v uv >/dev/null 2>&1; then
  uv tool install --force "$package"
  printf '\nsession-migrate installed. Run: session-migrate --help\n'
  exit 0
fi

python="${PYTHON:-python3}"
if ! command -v "$python" >/dev/null 2>&1; then
  printf '%s\n' 'session-migrate requires Python 3.11+ or uv.' >&2
  exit 1
fi

if ! "$python" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
  printf '%s\n' 'session-migrate requires Python 3.11 or newer.' >&2
  exit 1
fi

data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
bin_dir="${XDG_BIN_HOME:-$HOME/.local/bin}"
install_dir="${SESSION_MIGRATE_INSTALL_DIR:-$data_home/session-migrate/tool}"

mkdir -p "$install_dir" "$bin_dir"
for command_name in session-migrate smigrate; do
  destination="$bin_dir/$command_name"
  if [ -e "$destination" ] && [ ! -L "$destination" ]; then
    printf 'Refusing to replace existing file: %s\n' "$destination" >&2
    exit 1
  fi
done

"$python" -m venv "$install_dir"
"$install_dir/bin/python" -m pip install --disable-pip-version-check --upgrade "$package"

for command_name in session-migrate smigrate; do
  destination="$bin_dir/$command_name"
  ln -sfn "$install_dir/bin/$command_name" "$destination"
done

printf '\nsession-migrate installed in %s\n' "$install_dir"
printf 'Run: %s/session-migrate --help\n' "$bin_dir"
case ":${PATH:-}:" in
  *:"$bin_dir":*) ;;
  *) printf 'Add %s to PATH to invoke it directly.\n' "$bin_dir" ;;
esac
