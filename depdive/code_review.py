from package_locator.common import NPM
from version_differ.version_differ import FileDiff
from package_locator.locator import get_repository_url_and_subdir
from depdive.common import LineDelta, process_whitespace
from depdive.registry_diff import get_registry_version_diff
from depdive.repository_diff import (
    RepositoryDiff,
    SingleCommitFileChangeData,
    get_full_file_history,
    get_repository_file_list,
    git_blame,
    git_blame_delete,
)
from depdive.code_review_checker import CommitReviewInfo


class UncertainSubdir(Exception):
    """Cannot verify package directory at version commit"""

    pass


class LineCounterCommit:
    def __init__(self, commit: str):
        self.commit: str = commit
        self.file: str = None
        self.change: LineDelta = None


class LineCounter:
    def __init__(self, line: str):
        self.line: str = line
        self.registry_diff: LineDelta = None  # AddDelData
        self.mapped_commits: dict[str, LineCounterCommit] = {}  # each commit key maps to repo_file_name and
        self.registry_diff_error: LineDelta = None


class DepdiveStats:
    def __init__(self, reviewed_lines, non_reviewed_lines, total_commits, reviewed_commits) -> None:
        self.reviewed_lines = reviewed_lines
        self.non_reviewed_lines = non_reviewed_lines

        self.total_commits = total_commits
        self.reviewed_commits = reviewed_commits

    def print(self):
        print(self.reviewed_lines, self.non_reviewed_lines, self.total_commits, self.reviewed_commits)


class CodeReviewAnalysis:
    def __init__(self, ecosystem, package, old_version, new_version, repository=None, directory=None):
        self.ecosystem: str = ecosystem
        self.package: str = package
        self.old_version: str = old_version
        self.new_version: str = new_version

        self.repository: str = repository
        self.directory: str = directory
        if not self.repository:
            self._locate_repository()

        # these fields will be updated after analysis is run.
        # Is this a good design pattern?

        self.start_commit: str = None
        self.end_commit: str = None

        # line counter from version_differ output
        self.registry_diff = {}

        # Newly added phantom files:
        # files present in the registry, but not in repo
        self.phantom_files: dict[str, FileDiff] = {}
        # Newly added phantom lines:
        # files present in repo but contains lines
        # that are only present in registry
        self.phantom_lines: dict[str, dict[str, LineDelta]] = {}

        # c2c: code to commit mapping
        self.added_loc_to_commit_map = {}
        self.removed_loc_to_commit_map = {}

        # commit to review map
        self.commit_review_info: dict[str, CommitReviewInfo] = {}

        self.run_analysis()

    def _locate_repository(self):
        self.repository, self.directory = get_repository_url_and_subdir(self.ecosystem, self.package)

    def get_repo_path_from_registry_path(self, filepath):
        # put custom logic here
        if self.ecosystem == NPM and self.package.startswith("@babel") and filepath.startswith("lib/"):
            filepath = "src/" + filepath.removeprefix("lib/")

        subdir = self.directory.removeprefix("./").removesuffix("/")
        return subdir + "/" + filepath if subdir else filepath

    def _process_phantom_files(self, registry_diff, repo_file_list):
        """
        Phantom files: Files that are present in the registry,
                        but not in the source repository
        """
        phantom_files = {}
        for f in registry_diff.keys():
            repo_f = self.get_repo_path_from_registry_path(f)
            if registry_diff[f].target_file and repo_f not in repo_file_list:
                phantom_files[f] = registry_diff[f]
        self.phantom_files = phantom_files

    def _get_phantom_lines_in_a_file(self, registry_file_diff, repo_file_diff):
        p_repo_diff = {}
        for l in repo_file_diff.changed_lines.keys():
            p_l = process_whitespace(l)
            p_repo_diff[p_l] = p_repo_diff.get(p_l, LineDelta())
            p_repo_diff[p_l].add(repo_file_diff.changed_lines[l])

        phantom = {}
        for l in registry_file_diff:
            if l not in p_repo_diff or registry_file_diff[l].delta() != p_repo_diff[l].delta():
                phantom[l] = LineDelta()
                phantom[l].add(registry_file_diff[l])
                if l in p_repo_diff:
                    phantom[l].subtract(p_repo_diff[l])

        return phantom

    def check_package_directory_at_new_version_point(self, subdir):
        # TODO: will fail if directory has changed between the two versions
        if subdir != self.directory:
            if not subdir:
                raise UncertainSubdir
            else:
                self.directory = subdir

    def get_registry_file_line_counter(self, f):
        lc = {}
        for l in [process_whitespace(l) for l in f.added_lines]:
            lc[l] = lc.get(l, LineDelta())
            lc[l].additions += 1

        for l in [process_whitespace(l) for l in f.removed_lines]:
            lc[l] = lc.get(l, LineDelta())
            lc[l].deletions += 1

        return lc

    def _proccess_phantom_lines(self, registry_diff, repository_diff):
        for f in registry_diff.diff.keys():
            if not registry_diff.diff[f].target_file:
                continue

            repo_f = self.get_repo_path_from_registry_path(f)

            if not registry_diff.diff[f].source_file and (
                repo_f not in repository_diff.diff.keys() or not repository_diff.diff[repo_f].is_rename
            ):
                # possible explanation: newly included file to be published - get all the commits from the beginning
                repository_diff.diff[repo_f], repository_diff.single_diff[repo_f] = get_full_file_history(
                    repository_diff.repo_path, repo_f, end_commit=repository_diff.new_version_commit
                )

            registry_file_diff = self.get_registry_file_line_counter(registry_diff.diff[f])

            phantom_lines = self._get_phantom_lines_in_a_file(
                registry_file_diff, repository_diff.single_diff.get(repo_f, SingleCommitFileChangeData())
            )

            if phantom_lines:
                # try looking beyond the initial commit boundary
                has_commit_boundary_changed = repository_diff.check_beyond_commit_boundary(
                    repo_f,
                    phantom_lines,
                )

                if has_commit_boundary_changed:
                    phantom_lines = self._get_phantom_lines_in_a_file(
                        registry_file_diff, repository_diff.single_diff.get(repo_f, SingleCommitFileChangeData())
                    )

            if phantom_lines:
                self.phantom_lines[f] = phantom_lines

            self.registry_diff[f] = registry_file_diff

    def run_analysis(self):
        if not self.repository:
            self._locate_repository()

        registry_diff = get_registry_version_diff(self.ecosystem, self.package, self.old_version, self.new_version)
        repository_diff = RepositoryDiff(
            self.ecosystem, self.package, self.repository, self.old_version, self.new_version
        )

        # TODO: check subdir at common_start_point as well?
        self.check_package_directory_at_new_version_point(repository_diff.new_version_subdir)

        self._process_phantom_files(registry_diff.diff, repository_diff.new_version_file_list)
        for pf in self.phantom_files.keys():
            registry_diff.diff.pop(pf, None)
        self._proccess_phantom_lines(registry_diff, repository_diff)

        self.map_commit_to_added_lines(repository_diff, registry_diff)
        self.map_commit_to_removed_lines(repository_diff, registry_diff)

        self.start_commit = repository_diff.old_version_commit
        self.end_commit = repository_diff.new_version_commit

        for repo_f in repository_diff.diff.keys():
            for commit in repository_diff.diff[repo_f].commits:
                if commit not in self.commit_review_info:
                    self.commit_review_info[commit] = CommitReviewInfo(self.repository, commit)

    def map_commit_to_added_lines(self, repository_diff, registry_diff):
        for f in registry_diff.diff.keys():
            if not registry_diff.diff[f].target_file:
                continue

            repo_f = self.get_repo_path_from_registry_path(f)

            c2c = git_blame(repository_diff.repo_path, repo_f, repository_diff.new_version_commit)

            for commit in list(c2c.keys()):
                if commit not in repository_diff.diff[repo_f].commits:
                    c2c.pop(commit)
                else:
                    c2c[commit] = [process_whitespace(l) for l in c2c[commit]]
                    c2c[commit] = [l for l in c2c[commit] if l]
                    if not c2c[commit]:
                        c2c.pop(commit)

            self.added_loc_to_commit_map[f] = c2c

    def map_commit_to_removed_lines(self, repository_diff, registry_diff):
        starter_point_file_list = get_repository_file_list(
            repository_diff.repo_path, repository_diff.common_starter_commit
        )

        for f in registry_diff.diff.keys():
            repo_f = self.get_repo_path_from_registry_path(f)
            if not registry_diff.diff[f].source_file or repo_f not in starter_point_file_list:
                continue

            c2c = git_blame_delete(
                repository_diff.repo_path,
                repo_f,
                repository_diff.common_starter_commit,
                repository_diff.new_version_commit,
            )
            for k in list(c2c.keys()):
                c2c[k] = [process_whitespace(l) for l in c2c[k]]
                c2c[k] = [l for l in c2c[k] if l]
                if not c2c[k]:
                    c2c.pop(k)

            self.removed_loc_to_commit_map[f] = c2c

    def get_stats(self):
        reviewed_lines = non_reviewed_lines = 0
        non_reviewed_commits = set()
        reviewed_commits = set()

        for f in self.added_loc_to_commit_map.keys():
            for commit in self.added_loc_to_commit_map[f].keys():
                cur = len(self.added_loc_to_commit_map[f][commit])
                if self.commit_review_info[commit].review_category:
                    reviewed_lines += cur
                    reviewed_commits.add(commit)
                else:
                    non_reviewed_lines += cur
                    non_reviewed_commits.add(commit)

        for f in self.removed_loc_to_commit_map.keys():
            for commit in self.removed_loc_to_commit_map[f].keys():
                cur = len(self.removed_loc_to_commit_map[f][commit])
                if self.commit_review_info[commit].review_category:
                    reviewed_lines += cur
                    reviewed_commits.add(commit)
                else:
                    non_reviewed_lines += cur
                    non_reviewed_commits.add(commit)

        return DepdiveStats(
            reviewed_lines, non_reviewed_lines, len(reviewed_commits) + len(non_reviewed_commits), len(reviewed_commits)
        )
