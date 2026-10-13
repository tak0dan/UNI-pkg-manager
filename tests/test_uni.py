"""Tests for uni. Run with: python3 -m unittest discover -s tests -v"""

from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from uni import backends, config, detect, frontends, repology, resolver
from uni.backends import Backend, Step
from uni.model import Candidate, Intent, Passthrough
from uni.transaction import UnsafeCommand, check_safe, execute

PARSE_CTX = frontends.ParseCtx(
    known_sources=frozenset({"native", "flatpak", "snap", "aur", "pacman", "apt", "dnf", "zypper"}),
    remotes=lambda: {"flathub"},
)


def parse(syntax, *args):
    return frontends.parse(syntax, list(args), PARSE_CTX)


class FakeBackend(Backend):
    """In-memory backend: `table` maps exact name -> list of candidates."""

    needs_root = False

    def __init__(self, sid, table=None, installed=()):
        super().__init__()
        self.id = sid
        self.binary = sid
        self.table = table or {}
        self._installed = set(installed)

    def query(self, name):
        return list(self.table.get(name, []))

    def search(self, term):
        return [c for name, cs in self.table.items() if term in name for c in cs]

    def installed(self, ref):
        return ref in self._installed

    def install(self, cands, yes):
        return [Step([self.id, "install", *[c.ref for c in cands]])]

    def remove(self, cands, yes):
        return [Step([self.id, "remove", *[c.ref for c in cands]])]


def make_ctx(backends_map, pins=None, sources=None, db=None, native="pacman"):
    cfg = copy.deepcopy(config.DEFAULTS)
    if sources is not None:
        cfg["sources"] = sources
    cfg["pins"] = pins or {}
    repo = repology.Repology(enabled=False, cache=Path(tempfile.mkdtemp()), ttl_hours=1)
    return resolver.Context(cfg, backends_map, native, db or resolver.PackageDB(), repo)


class FrontendTests(unittest.TestCase):
    def test_apt_install_with_yes_and_flags(self):
        p = parse("apt", "-y", "install", "discord")
        self.assertIsInstance(p, Intent)
        self.assertEqual((p.op, p.terms, p.yes, p.syntax), ("install", ["discord"], True, "apt"))

    def test_apt_purge_is_remove(self):
        p = parse("apt", "purge", "foo")
        self.assertEqual(p.op, "remove")

    def test_unknown_flag_passes_through_unchanged(self):
        p = parse("apt", "install", "-o", "Dpkg::Options::=x", "foo")
        self.assertIsInstance(p, Passthrough)
        self.assertEqual(p.argv[0], "apt")

    def test_manager_only_command_passes_through(self):
        p = parse("flatpak", "remote-add", "x", "https://example")
        self.assertIsInstance(p, Passthrough)
        self.assertTrue(p.argv[0] == "flatpak")

    def test_pacman_install_search_remove(self):
        self.assertEqual(parse("pacman", "-S", "discord").op, "install")
        self.assertEqual(parse("pacman", "-Ss", "discord").op, "search")
        self.assertEqual(parse("pacman", "-R", "discord").op, "remove")

    def test_pacman_partial_upgrade_is_not_translated(self):
        # -Sy <pkg> must reach the safety check instead of being rewritten silently.
        self.assertIsInstance(parse("pacman", "-Sy", "discord"), Passthrough)

    def test_pacman_queries_are_read_only(self):
        self.assertTrue(parse("pacman", "-Qdt").read_only)
        self.assertFalse(parse("pacman", "-Syu").read_only)

    def test_flatpak_remote_positional_is_dropped(self):
        p = parse("flatpak", "install", "flathub", "com.discordapp.Discord")
        self.assertEqual(p.terms, ["com.discordapp.Discord"])

    def test_flatpak_from_file_flag_is_not_uni_source(self):
        p = parse("flatpak", "install", "--from", "app.flatpakref")
        self.assertIsInstance(p, Passthrough)

    def test_from_known_source_is_taken(self):
        p = parse("apt", "install", "--from", "flatpak", "discord")
        self.assertEqual(p.source, "flatpak")

    def test_uni_syntax_accepts_flags_anywhere(self):
        p = frontends.parse("uni", ["install", "discord", "--from", "snap", "--dry-run"], PARSE_CTX)
        self.assertEqual((p.op, p.terms, p.source, p.dry_run), ("install", ["discord"], "snap", True))


class ResolverTests(unittest.TestCase):
    def test_priority_picks_native_over_flatpak(self):
        bs = {
            "pacman": FakeBackend("pacman", {"discord": [Candidate("pacman", "discord")]}),
            "flatpak": FakeBackend("flatpak", {"com.discordapp.Discord": [Candidate("flatpak", "com.discordapp.Discord")]}),
        }
        db = resolver.PackageDB(aliases={"discord": {"flatpak": ["com.discordapp.Discord"]}})
        res = resolver.resolve("discord", make_ctx(bs, db=db))
        self.assertEqual(res.best.source, "pacman")
        self.assertFalse(res.tie)

    def test_pin_beats_priority(self):
        bs = {
            "pacman": FakeBackend("pacman", {"discord": [Candidate("pacman", "discord")]}),
            "flatpak": FakeBackend("flatpak", {"com.discordapp.Discord": [Candidate("flatpak", "com.discordapp.Discord")]}),
        }
        db = resolver.PackageDB(aliases={"discord": {"flatpak": ["com.discordapp.Discord"]}})
        res = resolver.resolve("discord", make_ctx(bs, pins={"discord": "flatpak"}, db=db))
        self.assertEqual(res.best.source, "flatpak")

    def test_same_priority_is_a_tie(self):
        bs = {"pacman": FakeBackend("pacman", {"discord": [Candidate("pacman", "discord"),
                                                           Candidate("pacman", "discord-git")]})}
        res = resolver.resolve("discord", make_ctx(bs))
        self.assertTrue(res.tie)
        self.assertEqual(len(res.ranked), 2)

    def test_from_is_exclusive_and_does_not_fall_back(self):
        bs = {
            "pacman": FakeBackend("pacman", {"discord": [Candidate("pacman", "discord")]}),
            "flatpak": FakeBackend("flatpak", {}),
        }
        res = resolver.resolve("discord", make_ctx(bs), source="flatpak")
        self.assertIsNone(res.best)
        self.assertEqual(res.code, resolver.EXIT_NOT_FOUND)

    def test_renamed_resolves_automatically(self):
        db = resolver.PackageDB(relations={"oldname": resolver.Relation("newname", "renamed")})
        bs = {"pacman": FakeBackend("pacman", {"newname": [Candidate("pacman", "newname")]})}
        res = resolver.resolve("oldname", make_ctx(bs, db=db))
        self.assertEqual(res.best.ref, "newname")
        self.assertIsNone(res.replacement)

    def test_fork_requires_confirmation(self):
        db = resolver.PackageDB(relations={"swww": resolver.Relation("awww", "fork")})
        bs = {"pacman": FakeBackend("pacman", {"awww": [Candidate("pacman", "awww")]})}
        res = resolver.resolve("swww", make_ctx(bs, db=db))
        self.assertEqual(res.replacement, "awww")
        self.assertEqual(res.best.ref, "awww")

    def test_remove_only_considers_installed(self):
        bs = {"pacman": FakeBackend("pacman", {"discord": [Candidate("pacman", "discord")]}, installed=())}
        res = resolver.resolve("discord", make_ctx(bs), installed_only=True)
        self.assertEqual(res.code, resolver.EXIT_NOT_FOUND)

    def test_disabled_source_is_skipped_without_from(self):
        cfg_sources = copy.deepcopy(config.DEFAULTS["sources"])
        cfg_sources["flatpak"]["enabled"] = False
        bs = {"flatpak": FakeBackend("flatpak", {"foo": [Candidate("flatpak", "foo")]})}
        res = resolver.resolve("foo", make_ctx(bs, sources=cfg_sources, native="pacman"))
        self.assertIsNotNone(res.error)
        res = resolver.resolve("foo", make_ctx(bs, sources=cfg_sources, native="pacman"), source="flatpak")
        self.assertEqual(res.best.source, "flatpak")


class TransactionTests(unittest.TestCase):
    def test_pacman_partial_upgrade_refused(self):
        with self.assertRaises(UnsafeCommand):
            check_safe(Step(["pacman", "-Sy", "discord"]))
        with self.assertRaises(UnsafeCommand):
            check_safe(Step(["pacman", "-S", "-y", "discord"]))

    def test_full_upgrade_and_refresh_only_allowed(self):
        check_safe(Step(["pacman", "-Syu", "discord"]))
        check_safe(Step(["pacman", "-Sy"]))
        check_safe(Step(["pacman", "-S", "--needed", "discord"]))

    def test_dry_run_prints_commands_and_runs_nothing(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["safety"]["snapper"] = False
        out: list[str] = []
        with mock.patch("subprocess.run") as run_mock:
            rc = execute([Step(["pacman", "-S", "discord"], root=False)], cfg, True, "x", echo=out.append)
        self.assertEqual(rc, 0)
        run_mock.assert_not_called()
        self.assertEqual(out, ["$ pacman -S discord"])

    def test_root_steps_get_sudo_prefix(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["safety"]["snapper"] = False
        out: list[str] = []
        with mock.patch("os.geteuid", return_value=1000):
            execute([Step(["pacman", "-S", "discord"], root=True)], cfg, True, "x", echo=out.append)
        self.assertEqual(out, ["$ sudo pacman -S discord"])

    def test_unsafe_step_refuses_whole_transaction(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        with mock.patch("subprocess.run") as run_mock:
            with self.assertRaises(UnsafeCommand):
                execute([Step(["pacman", "-Sy", "x"])], cfg, False, "x")
        run_mock.assert_not_called()


class BackendParsingTests(unittest.TestCase):
    def test_pacman_info_and_search(self):
        info = "Name            : discord\nVersion         : 1:1.0.161-1\nDescription     : All-in-one voice\n"
        search = "extra/discord 1:1.0.161-1 [installed]\n    All-in-one voice and text chat\nextra/foo 1.0\n    bar\n"
        b = backends.PacmanBackend()
        with mock.patch.object(backends, "run", return_value=(0, info)):
            [c] = b.query("discord")
        self.assertEqual((c.ref, c.version, c.description), ("discord", "1:1.0.161-1", "All-in-one voice"))
        with mock.patch.object(backends, "run", return_value=(0, search)):
            names = [c.ref for c in b.search("x")]
        self.assertEqual(names, ["discord", "foo"])

    def test_pacman_query_rejects_other_names(self):
        info = "Name            : discord-git\nVersion         : 1\n"
        with mock.patch.object(backends, "run", return_value=(0, info)):
            self.assertEqual(backends.PacmanBackend().query("discord"), [])

    def test_apt_show_and_search(self):
        show = "Package: discord\nVersion: 1.0\nDescription: chat\n"
        b = backends.AptBackend()
        with mock.patch.object(backends, "run", return_value=(0, show)):
            [c] = b.query("discord")
        self.assertEqual(c.version, "1.0")
        with mock.patch.object(backends, "run", return_value=(0, "discord - chat app\n")):
            [c] = b.search("disc")
        self.assertEqual((c.ref, c.description), ("discord", "chat app"))

    def test_apt_install_command(self):
        step = backends.AptBackend().install([Candidate("apt", "discord")], yes=True)[0]
        self.assertEqual(step.argv, ["apt", "install", "-y", "discord"])
        self.assertTrue(step.root)

    def test_flatpak_rows_and_remote_grouping(self):
        out = "Application ID\tVersion\tDescription\tRemotes\ncom.discordapp.Discord\t1.0\tchat\tflathub\n"
        b = backends.FlatpakBackend()
        with mock.patch.object(backends, "run", return_value=(0, out)):
            [c] = b.query("com.discordapp.Discord")
        self.assertEqual((c.remote, c.version), ("flathub", "1.0"))
        steps = b.install([c], yes=False)
        self.assertEqual(steps[0].argv, ["flatpak", "install", "flathub", "com.discordapp.Discord"])

    def test_zypper_search_table(self):
        out = ("S | Name    | Summary | Type\n--+---------+---------+--------\n"
               "  | discord | chat    | package\n  | discord-patterns | x | pattern\n")
        with mock.patch.object(backends, "run", return_value=(0, out)):
            found = backends.ZypperBackend().search("disc")
        self.assertEqual([c.ref for c in found], ["discord"])

    def test_dnf_repoquery_exact_match(self):
        out = "discord\t0.0.1-1.fc42\tchat\ndiscord-ptb\t0.1\tptb\n"
        with mock.patch.object(backends, "run", return_value=(0, out)):
            [c] = backends.DnfBackend().query("discord")
        self.assertEqual((c.ref, c.version, c.description), ("discord", "0.0.1-1.fc42", "chat"))

    def test_snap_find_columns(self):
        out = "Name     Version  Publisher  Notes  Summary\ndiscord  1.2      x          -      chat app here\n"
        with mock.patch.object(backends, "run", return_value=(0, out)):
            [c] = backends.SnapBackend().search("discord")
        self.assertEqual((c.ref, c.version, c.description), ("discord", "1.2", "chat app here"))

    def test_aur_query_uses_rpc_json(self):
        payload = {"results": [{"Name": "discord-git", "Version": "r1", "Description": "d"}]}
        fake = mock.MagicMock()
        fake.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        with mock.patch("urllib.request.urlopen", return_value=fake):
            [c] = backends.AurBackend({"helper": "paru"}).query("discord-git")
        self.assertEqual((c.source, c.ref), ("aur", "discord-git"))


class DetectTests(unittest.TestCase):
    def test_family_mapping(self):
        self.assertEqual(detect.native_backend({"ID": "garuda", "ID_LIKE": "arch"}, {"pacman"}), "pacman")
        self.assertEqual(detect.native_backend({"ID": "ubuntu", "ID_LIKE": "debian"}, {"apt"}), "apt")
        self.assertEqual(detect.native_backend({"ID": "fedora"}, {"dnf"}), "dnf")
        self.assertEqual(detect.native_backend({"ID": "opensuse-tumbleweed", "ID_LIKE": "opensuse suse"},
                                               {"zypper"}), "zypper")

    def test_unknown_distro_falls_back_to_installed_manager(self):
        self.assertEqual(detect.native_backend({"ID": "mystery"}, {"apt"}), "apt")


class RepologyTests(unittest.TestCase):
    def test_repo_mapping(self):
        self.assertEqual(repology.repo_to_backend("arch"), "pacman")
        self.assertEqual(repology.repo_to_backend("debian_12"), "apt")
        self.assertEqual(repology.repo_to_backend("fedora_42"), "dnf")
        self.assertEqual(repology.repo_to_backend("flathub"), "flatpak")
        self.assertIsNone(repology.repo_to_backend("gentoo"))

    def test_names_are_cached_then_reused(self):
        calls = []

        def fake_fetch(project):
            calls.append(project)
            return [{"repo": "arch", "name": "discord"}, {"repo": "aur", "name": "discord-git"},
                    {"repo": "gentoo", "name": "x"}]

        with tempfile.TemporaryDirectory() as tmp:
            rep = repology.Repology(True, Path(tmp), 24, fetch=fake_fetch)
            self.assertEqual(rep.names("discord"), [("pacman", "discord"), ("aur", "discord-git")])
            self.assertEqual(rep.names("discord"), [("pacman", "discord"), ("aur", "discord-git")])
        self.assertEqual(calls, ["discord"])

    def test_network_failure_is_a_warning_not_an_error(self):
        def broken(project):
            raise OSError("offline")

        with tempfile.TemporaryDirectory() as tmp:
            rep = repology.Repology(True, Path(tmp), 24, fetch=broken)
            self.assertEqual(rep.names("discord"), [])
            self.assertTrue(rep.warnings)


class ConfigTests(unittest.TestCase):
    def test_user_file_overrides_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('[sources.snap]\nenabled = true\npriority = 0\n[syntax]\nmode = "route"\n'
                            'route = ["flatpak"]\n', encoding="utf-8")
            cfg = config.load(path)
        self.assertTrue(cfg["sources"]["snap"]["enabled"])
        self.assertEqual(cfg["syntax"]["route"], ["flatpak"])
        self.assertEqual(cfg["sources"]["native"]["priority"], 1)

    def test_invalid_mode_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('[syntax]\nmode = "bogus"\n', encoding="utf-8")
            with self.assertRaises(config.ConfigError):
                config.load(path)

    def test_bundled_alias_table_loads(self):
        db = resolver.PackageDB.load([resolver.DATA_PACKAGES])
        self.assertEqual(db.aliases["discord"]["flatpak"], ["com.discordapp.Discord"])
        self.assertEqual(db.relations["swww"].kind, "fork")


@unittest.skipUnless(shutil.which("pacman"), "needs pacman")
class LivePacmanTests(unittest.TestCase):
    def test_real_pacman_query(self):
        self.assertEqual(backends.PacmanBackend().query("pacman")[0].ref, "pacman")

    def test_real_pacman_missing_package(self):
        self.assertEqual(backends.PacmanBackend().query("definitely-not-a-package-xyz"), [])


class TuiTests(unittest.TestCase):
    def test_picker_preselects_first_and_returns_it_on_enter(self):
        try:
            import textual  # noqa: F401
        except ImportError:
            self.skipTest("textual not installed")
        import asyncio

        from uni import tui

        cls = tui._app_class()
        options = [(Candidate("pacman", "discord"), "pacman -S discord"),
                   (Candidate("pacman", "discord-git"), "pacman -S discord-git")]

        async def drive():
            app = cls("discord", options)
            async with app.run_test() as pilot:
                await pilot.press("enter")
            return app.return_value

        self.assertEqual(asyncio.run(drive()).ref, "discord")


if __name__ == "__main__":
    unittest.main()
