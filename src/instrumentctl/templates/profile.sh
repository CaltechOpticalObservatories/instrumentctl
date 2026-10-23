# @display_name@ venv on PATH for every login shell, installed by `@name@ install`.
#
# PATH rather than `activate`: python, libby and the driver tools resolve to the
# venv, while VIRTUAL_ENV and the prompt stay free for a developer's own venv.

case ":$PATH:" in
    *":@venv@/bin:"*) ;;
    *) PATH="@venv@/bin:$PATH" ;;
esac
export PATH

# Sourced by sh as well as bash, and only bash understands the completion code
if [ -n "${BASH_VERSION:-}" ] && [ -x "@venv@/bin/libby" ]; then
    eval "$("@venv@/bin/libby" completion bash)"
fi
