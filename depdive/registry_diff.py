from sys import version
from package_locator.common import CARGO, PYPI
from version_differ.common import PIP
from version_differ.version_differ import get_version_diff_stats


class VersionDifferError(Exception):
    pass


def get_registry_version_diff(ecosystem, package, old, new):
    if ecosystem == PYPI:
        ecosystem = PIP

    version_diff = get_version_diff_stats(ecosystem, package, old, new)

    # preprocess auto-gen files respective to each registry
    if ecosystem == CARGO:
        preprocess_cargo_crate_files(version_diff)

    return version_diff


def preprocess_cargo_crate_files(version_diff):

    # handle Cargo's handling of Cargo.toml file
    if "Cargo.toml.orig" in version_diff.diff:
        version_diff.diff.pop("Cargo.toml", None)
        version_diff.diff["Cargo.toml"] = version_diff.diff["Cargo.toml.orig"]

        version_diff.diff.pop("Cargo.toml.orig", None)
        version_diff.new_version_filelist.discard("Cargo.toml.orig")
    else:
        raise VersionDifferError

    # filter out auto-generated files
    auto_gen_files = [".cargo_vcs_info.json", "Cargo.lock"]
    for f in auto_gen_files:
        version_diff.diff.pop(f, None)
        version_diff.new_version_filelist.discard(f)