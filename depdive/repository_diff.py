from git import Repo
from unidiff import PatchSet
from version_differ.version_differ import get_commit_of_release
import tempfile
from os.path import join, relpath
import os
from package_locator.directory import locate_subdir
from depdive.common import LineDelta, process_whitespace


class ReleaseCommitNotFound(Exception):
    def message():
        return "Release commit not found"


class SingleCommitFileChangeData:
    def __init__(self, file=None):
        self.source_file: str = file
        self.target_file: str = file
        self.is_rename: bool = False
        self.changed_lines: dict[str, LineDelta] = {}


class MultipleCommitFileChangeData:
    def __init__(self, filename):
        self.filename: str = filename

        # keeps track if it is a renamed file
        self.is_rename: bool = False
        self.old_name: str = None

        self.changed_lines: dict[str, dict[str, LineDelta]] = {}


def get_doubeledot_inbetween_commits(repo_path, commit_a, commit_b):
    repo = Repo(repo_path)
    commits = repo.iter_commits("{}..{}".format(commit_a, commit_b))
    return [str(c) for c in commits]


def get_all_commits_on_file(repo_path, filepath, start_commit=None, end_commit=None):
    # upto given commit
    repo = Repo(repo_path)

    if start_commit and end_commit:
        commits = repo.git.log(
            "{}^..{}".format(start_commit, end_commit), "--pretty=%H", "--follow", "--", filepath
        ).split("\n")
    elif start_commit:
        commits = repo.git.log("{}^..".format(start_commit), "--pretty=%H", "--follow", "--", filepath).split("\n")
    elif end_commit:
        commits = repo.git.log(end_commit, "--pretty=%H", "--follow", "--", filepath).split("\n")
    else:
        commits = repo.git.log("--pretty=%H", "--follow", "--", filepath).split("\n")

    return [c for c in commits if c]


def get_commit_diff(repo_path, commit, reverse=False):
    repo = Repo(repo_path)
    try:
        if not reverse:
            uni_diff_text = repo.git.diff(
                "{}~".format(commit), "{}".format(commit), ignore_blank_lines=True, ignore_space_at_eol=True
            )
        else:
            uni_diff_text = repo.git.diff(
                "{}".format(commit), "{}~".format(commit), ignore_blank_lines=True, ignore_space_at_eol=True
            )
    except:
        # Case 1: first commit, no parent
        uni_diff_text = repo.git.show("{}".format(commit), ignore_blank_lines=True, ignore_space_at_eol=True)

    return uni_diff_text


def get_commit_diff_for_file(repo_path, filepath, commit, reverse=False):
    repo = Repo(repo_path)
    try:
        if not reverse:
            uni_diff_text = repo.git.diff(
                "{}~".format(commit),
                "{}".format(commit),
                "--",
                filepath,
                ignore_blank_lines=True,
                ignore_space_at_eol=True,
            )
        else:
            uni_diff_text = repo.git.diff(
                "{}".format(commit),
                "{}~".format(commit),
                "--",
                filepath,
                ignore_blank_lines=True,
                ignore_space_at_eol=True,
            )
    except:
        # in case of first commit, no parent
        uni_diff_text = repo.git.show(
            "{}".format(commit), "--", filepath, ignore_blank_lines=True, ignore_space_at_eol=True
        )

    return uni_diff_text


def get_inbetween_commit_diff(repo_path, commit_a, commit_b):
    repo = Repo(repo_path)
    uni_diff_text = repo.git.diff(
        "{}".format(commit_a), "{}".format(commit_b), ignore_blank_lines=True, ignore_space_at_eol=True
    )
    return uni_diff_text


def get_inbetween_commit_diff_for_file(repo_path, filepath, commit_a, commit_b):
    assert commit_a != commit_b  # assumption

    repo = Repo(repo_path)
    uni_diff_text = repo.git.diff(
        "{}".format(commit_a),
        "{}".format(commit_b),
        "--",
        filepath,
        ignore_blank_lines=True,
        ignore_space_at_eol=True,
    )
    return uni_diff_text


def process_patch_filepath(filepath):
    filepath = filepath.removeprefix("a/")
    filepath = filepath.removeprefix("b/")
    if filepath == "/dev/null":
        filepath = None
    return filepath


def get_diff_files(uni_diff_text):
    patch_set = PatchSet(uni_diff_text)
    files = {}

    for patched_file in patch_set:
        f = SingleCommitFileChangeData()
        f.source_file = process_patch_filepath(patched_file.source_file)
        f.target_file = process_patch_filepath(patched_file.target_file)
        f.is_rename = patched_file.is_rename

        add_lines = [line.value for hunk in patched_file for line in hunk if line.is_added and line.value.strip()]

        del_lines = [line.value for hunk in patched_file for line in hunk if line.is_removed and line.value.strip()]

        for line in del_lines:
            f.changed_lines[line] = f.changed_lines.get(line, LineDelta())
            f.changed_lines[line].deletions += 1

        for line in add_lines:
            f.changed_lines[line] = f.changed_lines.get(line, LineDelta())
            f.changed_lines[line].additions += 1

        files[patched_file.path] = f

    return files


def get_commit_diff_stats_from_repo(repo_path, commits, reverse_commits=[]):
    files = {}

    for commit in commits + reverse_commits:
        diff = get_diff_files(get_commit_diff(repo_path, commit, reverse=commit in reverse_commits))
        for file in diff.keys():
            files[file] = files.get(file, MultipleCommitFileChangeData(file))

            if diff[file].is_rename:
                files[file].is_rename = True
                files[file].old_name = diff[file].source_file

            for line in diff[file].changed_lines.keys():
                files[file].changed_lines[line] = files[file].changed_lines.get(line, {})
                assert commit not in files[file].changed_lines[line]
                files[file].changed_lines[line][commit] = diff[file].changed_lines[line]

    def recurring_merge_rename(f):
        if files[f].is_rename and files[f].old_name in files.keys():
            old_f = files[f].old_name
            files[old_f] = recurring_merge_rename(old_f)
            for l in files[old_f].changed_lines.keys():
                if l not in files[f].changed_lines:
                    files[f].changed_lines[l] = files[old_f].changed_lines[l]
                else:
                    for c in files[old_f].changed_lines[l]:
                        if c not in files[f].changed_lines[l]:
                            files[f].changed_lines[l][c] = files[old_f].changed_lines[l][c]
        return files[f]

    # converge with old name in the case of renamed files
    for f in files.keys():
        files[f] = recurring_merge_rename(f)

    return files


def get_diff_file_commit_mapping(path, old_commit, new_commit):
    commits = get_doubeledot_inbetween_commits(path, old_commit, new_commit)
    reverse_commits = get_doubeledot_inbetween_commits(path, new_commit, old_commit)
    diff_file_commit_mapping = get_commit_diff_stats_from_repo(path, commits, reverse_commits)
    return diff_file_commit_mapping


def get_repository_file_list(repo_path, commit):
    repo = Repo(repo_path)
    head = repo.head.object.hexsha

    repo.git.checkout(commit)
    filelist = []
    for root, dirs, files in os.walk(repo_path):
        for file in files:
            filelist.append(relpath(join(root, file), repo_path))
    repo.git.checkout(head)

    return set(filelist)


def get_full_file_history(repo_path, filepath, end_commit="HEAD"):
    """ get commit history of filepath upto given commit point """
    commits = get_all_commits_on_file(repo_path, filepath, end_commit=end_commit)

    diff_commit_mapping = get_commit_diff_stats_from_repo(repo_path, commits)

    single_diff = SingleCommitFileChangeData(filepath)
    lines = get_file_lines(repo_path, end_commit, filepath)
    for l in lines:
        single_diff.changed_lines[l] = single_diff.changed_lines.get(l, LineDelta())
        single_diff.changed_lines[l].additions += 1

    return diff_commit_mapping[filepath], single_diff


def get_file_lines(repo_path, commit, filepath):
    repo = Repo(repo_path)
    head = repo.head.object.hexsha

    repo.git.checkout(commit)
    with open(join(repo_path, filepath), "r") as f:
        lines = f.readlines()
    repo.git.checkout(head)

    return lines


class RepositoryDiff:
    def __init__(self, ecosystem, package, repository, old_version, new_version):
        self.ecosystem = ecosystem
        self.package = package
        self.repository = repository
        self.old_version = old_version
        self.new_version = new_version

        self._temp_dir = None
        self.repo_path = None

        self.old_version_commit = None
        self.new_version_commit = None

        self.diff = None  # diff across individual commits
        self.new_version_file_list = None
        self.new_version_subdir = None  # package directory at the new version commit
        self.single_diff = None  # single diff from old to new

        self.build_repository_diff()

    def get_commit_of_release(self, version):
        repo = Repo(self.repo_path)
        tags = repo.tags
        c = get_commit_of_release(tags, self.package, version)
        if c:
            return str(c)

    def build_repository_diff(self):
        if not self.repo_path:
            self._temp_dir = tempfile.TemporaryDirectory()
            self.repo_path = self._temp_dir.name
            Repo.clone_from(self.repository, self.repo_path)

        if not self.old_version_commit or not self.new_version_commit:
            if not self.old_version_commit:
                self.old_version_commit = self.get_commit_of_release(self.old_version)
            if not self.new_version_commit:
                self.new_version_commit = self.get_commit_of_release(self.new_version)

            if not self.old_version_commit or not self.new_version_commit:
                raise ReleaseCommitNotFound

        self.diff = get_diff_file_commit_mapping(self.repo_path, self.old_version_commit, self.new_version_commit)
        self.new_version_file_list = get_repository_file_list(self.repo_path, self.new_version_commit)
        self.new_version_subdir = locate_subdir(self.ecosystem, self.package, self.repository, self.new_version_commit)
        self.single_diff = get_diff_files(
            get_inbetween_commit_diff(self.repo_path, self.old_version_commit, self.new_version_commit)
        )

    def check_beyond_commit_boundary(self, filepath, phantom_lines):
        """
        returns new possible commit boundary
        for the corner case where the version was wrongly tagged at some commit
        and the actual uploaded artifact contains one or more commits beyond
        the boundary pointed at by the version tag

        Note that, we expand our commit boundary very conservatively,
        if the immediate next commit outside the boundary on the fiven file
        does not address phantom lines, we quit.
        """

        new_version_commit, old_version_commit = self.new_version_commit, self.old_version_commit
        if not new_version_commit or old_version_commit:
            return False

        # first take a look at the commis afterward new_version_commits
        commits = get_all_commits_on_file(self.repo_path, filepath, start_commit=new_version_commit)[::-1]
        if commits:
            commits = commits[1:] if commits[0] == new_version_commit else commits
            for commit in commits:
                diff = get_diff_files(get_commit_diff(self.repo_path, commit))
                commit_outside_boundary = True  # assume this commit is outside the actual boundary
                if filepath in diff:
                    commit_diff = diff[filepath].changed_lines
                    for line in commit_diff.keys():
                        p_line = process_whitespace(line)
                        if p_line in phantom_lines.keys():
                            phantom_lines[p_line].subtract(commit_diff[line])
                            if phantom_lines[p_line].is_empty():
                                phantom_lines.pop(p_line)
                            new_version_commit = commit
                            commit_outside_boundary = False

                if commit_outside_boundary or not phantom_lines:
                    break

        commits = get_all_commits_on_file(self.repo_path, filepath, end_commit=old_version_commit)
        if commits:
            commits = commits[1:] if commits[0] == old_version_commit else commits
            for commit in commits:
                diff = get_diff_files(get_commit_diff(self.repo_path, commit))
                commit_outside_boundary = True  # assume this commit is outside the actual boundary
                if filepath in diff:
                    commit_diff = diff[filepath].changed_lines
                    for line in commit_diff.keys():
                        p_line = process_whitespace(line)
                        if p_line in phantom_lines.keys():
                            phantom_lines[p_line].subtract(commit_diff[line])
                            if phantom_lines[p_line].is_empty():
                                phantom_lines.pop(p_line)
                            old_version_commit = commit
                            commit_outside_boundary = False

                if commit_outside_boundary or not phantom_lines:
                    break

        if new_version_commit != self.new_version_commit or old_version_commit != self.old_version_commit:
            self.new_version_commit, self.old_version_commit = new_version_commit, old_version_commit
            self.build_repository_diff()
            return True
        else:
            return False
