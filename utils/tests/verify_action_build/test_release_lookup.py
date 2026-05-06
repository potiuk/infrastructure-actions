#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
#
from datetime import datetime, timezone
from unittest import mock

from verify_action_build.release_lookup import (
    format_release_time,
    get_release_or_commit_time,
    is_source_detached,
)


class TestFormatReleaseTime:
    def test_includes_full_weekday_name(self):
        # 2026-04-23 was a Thursday.
        ts = datetime(2026, 4, 23, 14, 32, 0, tzinfo=timezone.utc)
        assert format_release_time(ts) == "Thursday 2026-04-23 14:32 UTC"

    def test_each_weekday(self):
        # Spot-check every weekday in a single calendar week.
        # 2026-04-20 was a Monday.
        for offset, name in enumerate(
            ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        ):
            ts = datetime(2026, 4, 20 + offset, 9, 0, tzinfo=timezone.utc)
            assert format_release_time(ts).startswith(name)

    def test_no_relative_phrase_in_output(self):
        # The whole point: never embed "X days ago" since it'd rot if the
        # output is re-read later.
        ts = datetime(2026, 4, 23, 14, 32, 0, tzinfo=timezone.utc)
        out = format_release_time(ts)
        assert "ago" not in out
        assert "in the future" not in out

    def test_naive_timestamp_treated_as_utc(self):
        # A datetime without tzinfo is assumed UTC rather than crashing.
        ts = datetime(2026, 4, 23, 14, 32, 0)
        out = format_release_time(ts)
        assert "2026-04-23 14:32 UTC" in out
        assert out.startswith("Thursday")

    def test_non_utc_timezone_converted(self):
        # +02:00 input should be displayed as the equivalent UTC.
        from datetime import timedelta as _td
        tz = timezone(_td(hours=2))
        ts = datetime(2026, 4, 23, 16, 32, 0, tzinfo=tz)  # 14:32 UTC
        out = format_release_time(ts)
        assert out == "Thursday 2026-04-23 14:32 UTC"


class TestGetReleaseOrCommitTime:
    def test_returns_release_published_at_when_tag_has_release(self):
        with mock.patch(
            "verify_action_build.release_lookup._find_tags_for_commit",
            return_value=["v1.2.3", "v1"],
        ), mock.patch(
            "verify_action_build.release_lookup._release_published_at",
            side_effect=lambda o, r, t: (
                datetime(2026, 4, 23, 14, 32, tzinfo=timezone.utc)
                if t == "v1.2.3" else None
            ),
        ):
            result = get_release_or_commit_time("org", "repo", "a" * 40)
        assert result is not None
        ts, tag, source = result
        assert tag == "v1.2.3"
        assert source == "release"
        assert ts.year == 2026 and ts.month == 4 and ts.day == 23

    def test_falls_back_to_second_tag_when_first_has_no_release(self):
        with mock.patch(
            "verify_action_build.release_lookup._find_tags_for_commit",
            return_value=["v1-rolling", "v1.0.0"],
        ), mock.patch(
            "verify_action_build.release_lookup._release_published_at",
            side_effect=lambda o, r, t: (
                datetime(2026, 1, 1, tzinfo=timezone.utc) if t == "v1.0.0" else None
            ),
        ):
            result = get_release_or_commit_time("org", "repo", "a" * 40)
        assert result is not None
        ts, tag, source = result
        assert tag == "v1.0.0"
        assert source == "release"

    def test_falls_back_to_commit_date_when_no_release(self):
        with mock.patch(
            "verify_action_build.release_lookup._find_tags_for_commit",
            return_value=["v9.9.9"],
        ), mock.patch(
            "verify_action_build.release_lookup._release_published_at",
            return_value=None,
        ), mock.patch(
            "verify_action_build.release_lookup._commit_committer_date",
            return_value=datetime(2026, 3, 1, tzinfo=timezone.utc),
        ):
            result = get_release_or_commit_time("org", "repo", "a" * 40)
        assert result is not None
        ts, tag, source = result
        assert tag == "v9.9.9"
        assert source == "commit"

    def test_falls_back_to_commit_date_when_no_tags(self):
        with mock.patch(
            "verify_action_build.release_lookup._find_tags_for_commit",
            return_value=[],
        ), mock.patch(
            "verify_action_build.release_lookup._commit_committer_date",
            return_value=datetime(2026, 2, 14, tzinfo=timezone.utc),
        ):
            result = get_release_or_commit_time("org", "repo", "a" * 40)
        assert result is not None
        ts, tag, source = result
        assert tag is None
        assert source == "commit"

    def test_returns_none_when_everything_fails(self):
        with mock.patch(
            "verify_action_build.release_lookup._find_tags_for_commit",
            return_value=[],
        ), mock.patch(
            "verify_action_build.release_lookup._commit_committer_date",
            return_value=None,
        ):
            result = get_release_or_commit_time("org", "repo", "a" * 40)
        assert result is None


class TestIsSourceDetached:
    """The detector decides whether a tag commit lacks buildable source so
    the rebuild can fall back to the default-branch source commit the
    release was cut from.  Each scenario mocks the top-level tree names
    that would be returned by GitHub's git-tree API for the tag commit.
    """

    @staticmethod
    def _patch_tree(names):
        return mock.patch(
            "verify_action_build.release_lookup._tree_top_level_names",
            return_value=set(names),
        )

    def test_orphan_tag_with_package_json_is_source_detached(self):
        # benchmark-action/github-action-benchmark@v1.22.0 shape: the
        # release-tagging workflow ships ``package.json`` (consumers read
        # it; ``node_modules/`` resolves against it) but excludes the
        # ``src/`` source.  Pre-fix the ``not has_pkg`` requirement caused
        # this to be treated as source-bearing → rebuild produced an empty
        # tree → ``canonicalizeUnit.js`` showed up as "only in original".
        names = {
            ".gitignore", "action-types.yml", "action.yml",
            "dist", "node_modules", "package-lock.json", "package.json",
        }
        with self._patch_tree(names):
            assert is_source_detached("org", "repo", "a" * 40) is True

    def test_orphan_tag_without_package_json_is_source_detached(self):
        # The original PR #768 shape (``a6b95b7``): orphan tag with only
        # ``dist/`` and an action manifest.  Must continue to be detected.
        names = {"action.yml", "dist"}
        with self._patch_tree(names):
            assert is_source_detached("org", "repo", "a" * 40) is True

    def test_tree_with_src_directory_is_not_source_detached(self):
        # The common case: the tag points at the same commit as the
        # default branch and the source lives under ``src/`` next to the
        # built ``dist/``.  No fallback needed.
        names = {
            "action.yml", "dist", "src", "package.json", "tsconfig.json",
        }
        with self._patch_tree(names):
            assert is_source_detached("org", "repo", "a" * 40) is False

    def test_root_typescript_source_is_not_source_detached(self):
        # An action that keeps its source at the repo root (``index.ts``
        # next to ``action.yml``) and emits to ``dist/`` shouldn't be
        # treated as source-detached even though there's no ``src/``.
        names = {
            "action.yml", "dist", "index.ts", "package.json", "tsconfig.json",
        }
        with self._patch_tree(names):
            assert is_source_detached("org", "repo", "a" * 40) is False

    def test_composite_or_docker_action_without_dist_is_not_flagged(self):
        # Composite / docker actions don't ship a ``dist/`` tree at all.
        # Without ``dist/`` there's no "rebuilt artifact" to reconcile,
        # so the source-detached fallback is irrelevant.
        names = {"action.yml", "Dockerfile", "scripts"}
        with self._patch_tree(names):
            assert is_source_detached("org", "repo", "a" * 40) is False

    def test_sub_path_disables_detection(self):
        # Monorepo sub-actions typically keep build tooling at the repo
        # root, so a sub_path tree without ``src/`` or ``package.json``
        # is expected and isn't source-detached.  The detector should
        # short-circuit on any non-empty ``sub_path`` without consulting
        # the tree at all.
        with mock.patch(
            "verify_action_build.release_lookup._tree_top_level_names"
        ) as tree:
            assert (
                is_source_detached("org", "repo", "a" * 40, sub_path="install/foo")
                is False
            )
            tree.assert_not_called()

    def test_empty_tree_returns_false(self):
        # An API failure / empty tree shouldn't be classed as
        # source-detached — the fallback would just look up a non-existent
        # source commit and produce a confusing report.
        with self._patch_tree(set()):
            assert is_source_detached("org", "repo", "a" * 40) is False
