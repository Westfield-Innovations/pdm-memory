# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

from __future__ import annotations

from pathlib import Path

import pytest

from pdm_memory import BasePDMPlugin, Memory, PluginManager


@pytest.fixture
def mem(tmp_path: Path) -> Memory:
    m = Memory(
        store=str(tmp_path / "plugins.db"),
        user="test_user",
        autoload_plugins=False,
    )
    yield m
    m.close()


class _ProbePlugin(BasePDMPlugin):
    name = "probe"

    def __init__(self) -> None:
        super().__init__()
        self.installed = False
        self.saved_id: str | None = None

    def on_install(self) -> None:
        assert self.mem is not None
        self.installed = True
        mid = self.mem.save(
            "plugin probe fact",
            tags=["plugin", "probe", "test"],
            p_magnitude=55.0,
        )
        self.saved_id = mid
        assert isinstance(self.mem.detect_torsion(), list)


class _SledgehammerPlugin(BasePDMPlugin):
    name = "sledgehammer"
    version = "1.0.0"

    def __init__(self) -> None:
        super().__init__()
        self.pre_calls: list[str] = []
        self.post_calls: list[str] = []
        self.hooks = {
            "pre_save": self._on_pre_save,
            "post_save": [self._on_post_save],
        }

    def _on_pre_save(self, sig):
        self.pre_calls.append(sig.id[:8])
        sig.metadata = {**(sig.metadata or {}), "sledge": True}
        return sig

    def _on_post_save(self, sig, memory_id: str) -> None:
        self.post_calls.append(memory_id)

    def crush(self) -> str:
        assert self.mem is not None
        return self.mem.save(
            "sledge fact",
            tags=["sledge", "hammer", "test"],
            p_magnitude=90.0,
        )


class TestUseConnector:
    def test_use_binds_attr_and_hooks(self, mem: Memory) -> None:
        plugin = _SledgehammerPlugin()
        returned = mem.use(plugin)

        assert returned is mem
        assert mem.sledgehammer is plugin
        from pdm_memory import PluginMemoryProxy

        assert isinstance(plugin.mem, PluginMemoryProxy)
        from pdm_memory.plugins.proxy import as_memory

        assert as_memory(plugin.mem) is mem
        assert not hasattr(PluginMemoryProxy, "unwrap")
        assert mem.plugins["sledgehammer"] is plugin
        assert plugin.bound_hooks == ["pre_save", "post_save"]
        assert plugin.load_source == "manual"

        mid = mem.save(
            "hooked save",
            tags=["alpha", "beta", "gamma"],
            p_magnitude=10.0,
        )
        assert plugin.pre_calls
        assert plugin.post_calls == [mid]

        rec = mem._storage.get(mid, user="test_user")
        assert rec is not None
        assert rec.metadata.get("sledge") is True

        via_plugin = mem.sledgehammer.crush()
        assert via_plugin

    def test_status_lists_plugins_and_hooks(self, mem: Memory) -> None:
        mem.use(_SledgehammerPlugin())
        report = mem.status(color=False)

        assert report.alive is True
        assert report.user == "test_user"
        assert report.store == "SQLiteDriver"
        assert report.sdk_version
        assert report.memory_count == 0
        assert len(report.plugins) == 1

        entry = report.plugins[0]
        assert entry.name == "sledgehammer"
        assert entry.version == "1.0.0"
        assert entry.hooks == ["pre_save", "post_save"]
        assert entry.source == "manual"

        plain = report.render(color=False)
        assert "[Plugin] sledgehammer v1.0.0" in plain
        assert "Hooks: pre_save, post_save" in plain
        assert "Source: manual" in plain
        assert "ALIVE" in plain
        assert "\033[" not in plain

        colored = report.render(color=True)
        assert "\033[" in colored
        assert "sledgehammer" in colored

        as_dict = report.as_dict()
        assert as_dict["plugins"][0]["name"] == "sledgehammer"
        assert as_dict["plugins"][0]["source"] == "manual"
        assert as_dict["alive"] is True

    def test_status_empty_plugins(self, mem: Memory) -> None:
        report = mem.status(color=False)
        assert report.plugins == []
        assert "(none — brain is stock)" in report.render(color=False)

    def test_use_duplicate_fails_fast(self, mem: Memory) -> None:
        mem.use(_SledgehammerPlugin())
        with pytest.raises(ValueError, match="already installed"):
            mem.use(_SledgehammerPlugin())

    def test_use_rejects_name_collision_with_memory_api(self, mem: Memory) -> None:
        class SavePlugin(BasePDMPlugin):
            name = "save"

        with pytest.raises(ValueError, match="collides"):
            mem.use(SavePlugin())

    def test_use_rejects_non_plugin(self, mem: Memory) -> None:
        with pytest.raises(TypeError):
            mem.use(object())  # type: ignore[arg-type]


class TestPluginDependencies:
    def test_requires_missing_fails_with_clear_message(self, mem: Memory) -> None:
        class LifeRadar(BasePDMPlugin):
            name = "LifeRadar"
            requires = ["GeoTagger"]

        with pytest.raises(
            ValueError,
            match=(
                r"Plugin 'LifeRadar' requires 'GeoTagger'\. "
                r"Please install it first"
            ),
        ):
            mem.use(LifeRadar())

    def test_requires_version_constraint(self, mem: Memory) -> None:
        class GeoTagger(BasePDMPlugin):
            name = "GeoTagger"
            version = "1.0.0"

        class LifeRadar(BasePDMPlugin):
            name = "LifeRadar"
            requires = ["GeoTagger>=1.2.0"]

        mem.use(GeoTagger())
        with pytest.raises(
            ValueError,
            match=r"requires 'GeoTagger>=1\.2\.0' \(installed 1\.0\.0\)",
        ):
            mem.use(LifeRadar())

        # Upgrade in place: unload and reinstall newer version.
        mem.unload("GeoTagger")

        class GeoTaggerV2(BasePDMPlugin):
            name = "GeoTagger"
            version = "1.2.0"

        mem.use(GeoTaggerV2()).use(LifeRadar())
        assert "LifeRadar" in mem.plugins

    def test_requires_satisfied_installs(self, mem: Memory) -> None:
        class GeoTagger(BasePDMPlugin):
            name = "GeoTagger"

        class LifeRadar(BasePDMPlugin):
            name = "LifeRadar"
            requires = ["GeoTagger"]

            def ping_geo(self) -> str:
                assert self.mem is not None
                return type(self.mem.GeoTagger).__name__

        mem.use(GeoTagger()).use(LifeRadar())
        assert "GeoTagger" in mem.plugins
        assert "LifeRadar" in mem.plugins
        assert mem.LifeRadar.ping_geo() == "GeoTagger"

    def test_requires_multiple_missing(self, mem: Memory) -> None:
        class LifeRadar(BasePDMPlugin):
            name = "LifeRadar"
            requires = ["GeoTagger", "WeatherFeed"]

        with pytest.raises(
            ValueError,
            match=(
                r"Plugin 'LifeRadar' requires 'GeoTagger', 'WeatherFeed'\. "
                r"Please install them first"
            ),
        ):
            mem.use(LifeRadar())

    def test_requires_self_rejected(self, mem: Memory) -> None:
        class Narcissus(BasePDMPlugin):
            name = "Narcissus"
            requires = ["Narcissus"]

        with pytest.raises(ValueError, match="cannot require itself"):
            mem.use(Narcissus())

    def test_autoload_orders_dependencies(self, mem: Memory, tmp_path: Path) -> None:
        plugin_dir = tmp_path / "deps"
        plugin_dir.mkdir()
        (plugin_dir / "a_radar.py").write_text(
            """
from pdm_memory.plugins.base import BasePDMPlugin

class LifeRadar(BasePDMPlugin):
    name = "LifeRadar"
    requires = ["GeoTagger"]
""",
            encoding="utf-8",
        )
        (plugin_dir / "b_geo.py").write_text(
            """
from pdm_memory.plugins.base import BasePDMPlugin

class GeoTagger(BasePDMPlugin):
    name = "GeoTagger"
""",
            encoding="utf-8",
        )
        installed = mem._plugin_manager.autoload(plugin_dir)
        names = [type(p).name for p in installed]
        assert names == ["GeoTagger", "LifeRadar"]
        assert "LifeRadar" in mem.plugins
        assert "GeoTagger" in mem.plugins


class TestUnloadPlugin:
    def test_unload_removes_hooks_and_attr(self, mem: Memory) -> None:
        class Guard(BasePDMPlugin):
            name = "guard"
            version = "1.0.0"

            def __init__(self) -> None:
                super().__init__()
                self.hooks = {"pre_save": self._pre}
                self.calls = 0

            def _pre(self, sig):
                self.calls += 1
                return sig

            def on_uninstall(self) -> None:
                self.uninstalled = True

        mem.use(Guard())
        assert mem.guard is mem.plugins["guard"]
        mem.save("alpha fact", tags=["a", "b", "c"])
        assert mem.guard.calls == 1

        assert mem.unload("guard") is True
        assert "guard" not in mem.plugins
        assert not hasattr(mem, "guard")

        mem.save("beta fact", tags=["a", "b", "c"])
        # No crash; hook is gone (would call removed plugin otherwise).

    def test_unload_blocked_by_dependents(self, mem: Memory) -> None:
        class GeoTagger(BasePDMPlugin):
            name = "GeoTagger"

        class LifeRadar(BasePDMPlugin):
            name = "LifeRadar"
            requires = ["GeoTagger"]

        mem.use(GeoTagger()).use(LifeRadar())
        with pytest.raises(ValueError, match="still required by"):
            mem.unload("GeoTagger")
        mem.unload("LifeRadar")
        assert mem.unload("GeoTagger") is True

    def test_disable_alias(self, mem: Memory) -> None:
        class Ping(BasePDMPlugin):
            name = "ping"

        mem.use(Ping())
        assert mem.disable("ping") is True
        assert mem.disable("ping") is False


class TestInstallPlugin:
    def test_install_sets_mem_and_runs_on_install(self, mem: Memory) -> None:
        plugin = mem.install_plugin(_ProbePlugin)
        assert plugin.installed is True
        from pdm_memory import PluginMemoryProxy

        assert isinstance(plugin.mem, PluginMemoryProxy)
        from pdm_memory.plugins.proxy import as_memory

        assert as_memory(plugin.mem) is mem
        assert plugin.saved_id is not None
        assert mem.plugins["probe"] is plugin
        assert mem.probe is plugin
        rec = mem._storage.get(plugin.saved_id, user="test_user")
        assert rec is not None
        assert "plugin probe" in rec.compressed_fact

    def test_duplicate_name_fails_fast(self, mem: Memory) -> None:
        mem.install_plugin(_ProbePlugin)
        with pytest.raises(ValueError, match="already installed"):
            mem.install_plugin(_ProbePlugin)

    def test_rejects_base_class(self, mem: Memory) -> None:
        with pytest.raises(TypeError):
            mem.install_plugin(BasePDMPlugin)  # type: ignore[arg-type]

    def test_rejects_non_plugin(self, mem: Memory) -> None:
        with pytest.raises(TypeError):
            mem.install_plugin(object)  # type: ignore[arg-type]


class TestPluginDiscovery:
    def test_discover_from_directory(self, mem: Memory, tmp_path: Path) -> None:
        plugin_dir = tmp_path / "ext_plugins"
        plugin_dir.mkdir()
        (plugin_dir / "echo_plugin.py").write_text(
            """
from pdm_memory.plugins.base import BasePDMPlugin

class EchoPlugin(BasePDMPlugin):
    name = "echo"

    def on_install(self):
        self.flag = True
""",
            encoding="utf-8",
        )
        manager = PluginManager(mem)
        classes = manager.discover(plugin_dir)
        assert len(classes) == 1
        assert classes[0].__name__ == "EchoPlugin"

        instance = manager.install(classes[0])
        assert mem.echo is instance
        assert getattr(instance, "flag", False) is True
        from pdm_memory import PluginMemoryProxy

        assert isinstance(instance.mem, PluginMemoryProxy)
        from pdm_memory.plugins.proxy import as_memory

        assert as_memory(instance.mem) is mem
        assert instance.load_source == "manual"

    def test_autoload_builtin_plugins_dir_is_safe(self, tmp_path: Path) -> None:
        m = Memory(store=str(tmp_path / "auto.db"), user="u", autoload_plugins=True)
        try:
            assert isinstance(m.plugins, dict)
            # Package plugins/ currently has no concrete autoload plugins.
            assert m._plugin_manager.discover() == []
        finally:
            m.close()

    def test_skips_invalid_module(self, mem: Memory, tmp_path: Path) -> None:
        plugin_dir = tmp_path / "broken"
        plugin_dir.mkdir()
        (plugin_dir / "broken_plugin.py").write_text(
            "raise RuntimeError('boom')\n",
            encoding="utf-8",
        )
        manager = PluginManager(mem)
        assert manager.discover(plugin_dir) == []


class TestExternalPluginFolders:
    def _write_manifested_plugin(
        self,
        plugin_dir: Path,
        *,
        name: str,
        class_name: str = "EchoPlugin",
        version: str = "0.1.0",
        requires: list[str] | None = None,
        autoload: bool = True,
        module: str = "echo_plugin",
        pin_sha256: bool = True,
        bad_sha256: str | None = None,
    ) -> Path:
        import json

        from pdm_memory.plugins.manifest import sha256_file

        plugin_dir.mkdir(parents=True, exist_ok=True)
        entry = plugin_dir / f"{module}.py"
        entry.write_text(
            f"""
from pdm_memory.plugins.base import BasePDMPlugin

class {class_name}(BasePDMPlugin):
    def on_install(self):
        self.ready = True
""",
            encoding="utf-8",
        )
        manifest: dict = {
            "name": name,
            "version": version,
            "entrypoint": f"{module}:{class_name}",
            "requires": requires or [],
            "autoload": autoload,
        }
        if bad_sha256 is not None:
            manifest["entrypoint_sha256"] = bad_sha256
        elif pin_sha256:
            manifest["entrypoint_sha256"] = sha256_file(entry)
        (plugin_dir / "plugin.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        return entry

    def test_find_external_plugin_dirs_walks_parents(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        app = workspace / "app"
        plugin_dir = workspace / "pdm-memory-plugin-echo"
        app.mkdir(parents=True)
        plugin_dir.mkdir()
        (plugin_dir / "echo.py").write_text("# placeholder\n", encoding="utf-8")

        found = PluginManager.find_external_plugin_dirs(app, walk_ancestors=True)
        assert plugin_dir.resolve() in found
        assert (
            PluginManager.find_external_plugin_dirs(app, walk_ancestors=False) == []
        )

    def test_autoload_external_from_cwd_tree(self, tmp_path: Path) -> None:
        workspace = tmp_path / "project"
        app = workspace / "src"
        plugin_dir = workspace / "pdm-memory-plugin-echo"
        app.mkdir(parents=True)
        self._write_manifested_plugin(plugin_dir, name="echo")

        m = Memory(store=str(tmp_path / "ext2.db"), user="u", autoload_plugins=False)
        try:
            classes = m._plugin_manager.discover_all(
                external_start=app,
                include_builtin=False,
                include_external=True,
            )
            # Fail closed — no allowlist / trust
            assert classes == []

            # Parent-tree layout requires allowlist (trust_plugins is cwd-only).
            m2 = Memory(
                store=str(tmp_path / "ext2b.db"),
                user="u",
                autoload_plugins=False,
                plugin_allowlist=[str(plugin_dir)],
            )
            try:
                classes = m2._plugin_manager.discover_all(
                    external_start=app,
                    include_builtin=False,
                    include_external=True,
                )
                assert len(classes) == 1
                assert classes[0].name == "echo"
                assert classes[0].version == "0.1.0"
                # Imported class ClassVars must remain untouched.
                base = classes[0].__bases__[0]
                assert base.__name__ == "EchoPlugin"
                m2._plugin_manager._install_discovered(classes)
                assert "echo" in m2.plugins
                assert getattr(m2.echo, "ready", False) is True
                assert m2.echo.load_source.startswith("external:")
                assert str(plugin_dir.resolve()) in m2.echo.load_source
            finally:
                m2.close()
        finally:
            m.close()

    def test_missing_manifest_fails_fast(self, tmp_path: Path) -> None:
        from pdm_memory import PluginManifestError

        workspace = tmp_path / "ws"
        plugin_dir = workspace / "pdm-memory-plugin-broken"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "x.py").write_text(
            "from pdm_memory.plugins.base import BasePDMPlugin\n"
            "class X(BasePDMPlugin):\n    name='x'\n",
            encoding="utf-8",
        )
        m = Memory(store=str(tmp_path / "bad.db"), user="u", autoload_plugins=False)
        try:
            assert (
                m._plugin_manager.discover_all(
                    external_start=workspace,
                    include_builtin=False,
                )
                == []
            )
            with pytest.warns(DeprecationWarning, match="trust_plugins"):
                m2 = Memory(
                    store=str(tmp_path / "bad2.db"),
                    user="u",
                    autoload_plugins=False,
                    trust_plugins=True,
                )
            try:
                with pytest.raises(PluginManifestError, match="missing plugin.json"):
                    m2._plugin_manager.discover_all(
                        external_start=workspace,
                        include_builtin=False,
                    )
            finally:
                m2.close()
        finally:
            m.close()

    def test_autoload_default_includes_external(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        workspace = tmp_path / "root"
        plugin_dir = workspace / "pdm-memory-plugin-ping"
        self._write_manifested_plugin(
            plugin_dir, name="ping", class_name="PingPlugin", module="ping"
        )
        monkeypatch.chdir(workspace)
        with pytest.warns(DeprecationWarning, match="trust_plugins"):
            m = Memory(
                store=str(tmp_path / "ping.db"),
                user="u",
                autoload_plugins=True,
                trust_plugins=True,
            )
        try:
            assert "ping" in m.plugins
            assert m.ping.load_source.startswith("external:")
        finally:
            m.close()

    def test_trust_plugins_does_not_walk_parents(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        workspace = tmp_path / "root"
        nested = workspace / "app"
        nested.mkdir(parents=True)
        plugin_dir = workspace / "pdm-memory-plugin-up"
        self._write_manifested_plugin(plugin_dir, name="up")
        monkeypatch.chdir(nested)
        with pytest.warns(DeprecationWarning, match="trust_plugins"):
            m = Memory(
                store=str(tmp_path / "up.db"),
                user="u",
                autoload_plugins=True,
                trust_plugins=True,
            )
        try:
            assert "up" not in m.plugins
        finally:
            m.close()

    def test_entrypoint_sha256_mismatch_fails(self, tmp_path: Path) -> None:
        from pdm_memory import PluginManifestError

        plugin_dir = tmp_path / "pdm-memory-plugin-evil"
        self._write_manifested_plugin(
            plugin_dir,
            name="evil",
            bad_sha256="0" * 64,
        )
        m = Memory(
            store=str(tmp_path / "evil.db"),
            user="u",
            autoload_plugins=False,
            plugin_allowlist=[str(plugin_dir)],
        )
        try:
            with pytest.raises(PluginManifestError, match="sha256 mismatch"):
                m._plugin_manager.load_from_manifest(plugin_dir)
        finally:
            m.close()

    def test_load_does_not_mutate_sys_path(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import sys

        plugin_dir = tmp_path / "pdm-memory-plugin-path"
        self._write_manifested_plugin(plugin_dir, name="path_check")
        before = list(sys.path)
        m = Memory(
            store=str(tmp_path / "path.db"),
            user="u",
            autoload_plugins=False,
            plugin_allowlist=[str(plugin_dir)],
        )
        try:
            m._plugin_manager.load_from_manifest(plugin_dir)
            assert sys.path == before
            assert str(plugin_dir.resolve()) not in sys.path
        finally:
            m.close()

    def test_default_skips_untrusted_external(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv("PDM_TRUST_PLUGINS", raising=False)
        workspace = tmp_path / "root"
        plugin_dir = workspace / "pdm-memory-plugin-ping"
        self._write_manifested_plugin(
            plugin_dir, name="ping", class_name="PingPlugin", module="ping"
        )
        monkeypatch.chdir(workspace)
        m = Memory(
            store=str(tmp_path / "ping_closed.db"),
            user="u",
            autoload_plugins=True,
            trust_plugins=False,
        )
        try:
            assert "ping" not in m.plugins
        finally:
            m.close()

    def test_plugin_allowlist_trusts_path(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("PDM_TRUST_PLUGINS", raising=False)
        workspace = tmp_path / "root"
        plugin_dir = workspace / "pdm-memory-plugin-allow"
        self._write_manifested_plugin(
            plugin_dir, name="allow_me", class_name="AllowPlugin", module="allow"
        )
        monkeypatch.chdir(workspace)
        m = Memory(
            store=str(tmp_path / "allow.db"),
            user="u",
            autoload_plugins=True,
            trust_plugins=False,
            plugin_allowlist=[str(plugin_dir)],
        )
        try:
            assert "allow_me" in m.plugins
        finally:
            m.close()

class TestPluginStorage:
    def test_plugin_drawer_isolation(self, mem: Memory) -> None:
        class Sledgehammer(BasePDMPlugin):
            name = "sledgehammer"

        mem.use(Sledgehammer())
        plugin = mem.sledgehammer

        assert plugin.plugin_drawer == "plugin:sledgehammer"

        chat_id = mem.save(
            "user prefers dark mode",
            tags=["pref", "ui", "theme"],
            drawer="general",
        )
        plugin_id = plugin.plugin_save(
            "scraper api key=sk-test",
            tags=["plugin", "sledgehammer", "secret"],
            p_magnitude=80.0,
        )

        page = plugin.plugin_list(limit=50)
        ids = {h.id for h in page.items}
        assert plugin_id in ids
        assert chat_id not in ids

        hits = plugin.plugin_recall("scraper api key", k=5, reinforce=False)
        assert any(h.id == plugin_id for h in hits)
        assert all(h.drawer == "plugin:sledgehammer" for h in hits)

        # Chat recall scoped to general must not see plugin drawer.
        general_hits = mem.recall(
            "scraper api key",
            k=5,
            drawer="general",
            reinforce=False,
        )
        assert all(h.id != plugin_id for h in general_hits)

    def test_plugin_save_rejects_drawer_override(self, mem: Memory) -> None:
        class Sledgehammer(BasePDMPlugin):
            name = "sledgehammer"

        mem.use(Sledgehammer())
        with pytest.raises(ValueError, match="drawer="):
            mem.sledgehammer.plugin_save("x", drawer="general")

    def test_plugin_delete_guards_foreign_drawer(self, mem: Memory) -> None:
        class Sledgehammer(BasePDMPlugin):
            name = "sledgehammer"

        mem.use(Sledgehammer())
        foreign = mem.save(
            "chat memory",
            tags=["chat", "alpha", "beta"],
            drawer="general",
        )
        owned = mem.sledgehammer.plugin_save(
            "owned config",
            tags=["plugin", "sledgehammer", "config"],
        )

        with pytest.raises(ValueError, match="not this plugin"):
            mem.sledgehammer.plugin_delete(foreign)

        assert mem.sledgehammer.plugin_delete(owned) is True
        assert mem.sledgehammer.plugin_get(owned) is None

    def test_plugin_get_hides_foreign(self, mem: Memory) -> None:
        class Sledgehammer(BasePDMPlugin):
            name = "sledgehammer"

        mem.use(Sledgehammer())
        foreign = mem.save(
            "chat memory",
            tags=["chat", "alpha", "beta"],
            drawer="general",
        )
        assert mem.sledgehammer.plugin_get(foreign) is None

    def test_unbound_plugin_save_fails(self) -> None:
        class Sledgehammer(BasePDMPlugin):
            name = "sledgehammer"

        plugin = Sledgehammer()
        with pytest.raises(RuntimeError, match="not bound"):
            plugin.plugin_save("nope")


class TestPluginProxyAndPriority:
    def test_proxy_denies_close_and_storage(self, mem: Memory) -> None:
        from pdm_memory import PluginCapabilityError

        class Nosy(BasePDMPlugin):
            name = "nosy"

            def poke(self) -> None:
                assert self.mem is not None
                self.mem.close()  # type: ignore[attr-defined]

        mem.use(Nosy())
        with pytest.raises(PluginCapabilityError, match="capability denied"):
            mem.nosy.poke()

        with pytest.raises(PluginCapabilityError, match="private"):
            _ = mem.nosy.mem._storage  # type: ignore[attr-defined]

    def test_proxy_allows_penalize_and_denies_plugins_dict(
        self, mem: Memory
    ) -> None:
        from pdm_memory import PluginCapabilityError

        class Signal(BasePDMPlugin):
            name = "signal"

            def down(self, memory_id: str) -> None:
                assert self.mem is not None
                self.mem.penalize(memory_id)

            def leak(self) -> None:
                assert self.mem is not None
                _ = self.mem.plugins

        mid = mem.save("penalty target fact", tags=["a", "b", "c"], p_magnitude=80.0)
        mem.use(Signal())
        mem.signal.down(mid)
        rec = mem._storage.get(mid, user="test_user")
        assert rec is not None
        assert rec.validation_prediction_total == 1
        assert rec.validation_prediction_correct == 0

        with pytest.raises(PluginCapabilityError, match="plugins"):
            mem.signal.leak()

    def test_proxy_denies_admin_io_by_default(
        self, mem: Memory, tmp_path: Path
    ) -> None:
        from pdm_memory import PluginCapabilityError
        from pdm_memory.plugins.proxy import CAP_ADMIN_IO, DEFAULT_CAPABILITIES

        class DefaultPlugin(BasePDMPlugin):
            name = "default_caps"

            def dump(self, path: Path) -> None:
                assert self.mem is not None
                self.mem.export_json(path)

        class IoPlugin(BasePDMPlugin):
            name = "io_caps"
            capabilities = DEFAULT_CAPABILITIES | {CAP_ADMIN_IO}

            def dump(self, path: Path) -> int:
                assert self.mem is not None
                return self.mem.export_json(path)

        mem.use(DefaultPlugin())
        out = tmp_path / "leak.json"
        with pytest.raises(PluginCapabilityError, match="admin_io"):
            mem.default_caps.dump(out)
        assert not out.exists()

        mem.use(IoPlugin())
        mem.save("seed", tags=["a", "b", "c"])
        assert mem.io_caps.dump(tmp_path / "ok.json") >= 1

    def test_proxy_denies_peer_without_capability(self, mem: Memory) -> None:
        from pdm_memory import PluginCapabilityError
        from pdm_memory.plugins.proxy import CAP_PEER, DEFAULT_CAPABILITIES

        class Peer(BasePDMPlugin):
            name = "peer_target"

            def ping(self) -> str:
                return "pong"

        class Stranger(BasePDMPlugin):
            name = "stranger_target"

            def ping(self) -> str:
                return "hi"

        class Caller(BasePDMPlugin):
            name = "caller"

            def call_peer(self) -> str:
                assert self.mem is not None
                return self.mem.peer_target.ping()

        class DepCaller(BasePDMPlugin):
            name = "dep_caller"
            requires = ["peer_target"]

            def call_peer(self) -> str:
                assert self.mem is not None
                return self.mem.peer_target.ping()

            def call_stranger(self) -> str:
                assert self.mem is not None
                return self.mem.stranger_target.ping()

        class TrustedCaller(BasePDMPlugin):
            name = "trusted_caller"
            capabilities = DEFAULT_CAPABILITIES | {CAP_PEER}

            def call_stranger(self) -> str:
                assert self.mem is not None
                return self.mem.stranger_target.ping()

        mem.use(Peer()).use(Stranger()).use(Caller()).use(DepCaller()).use(
            TrustedCaller()
        )
        with pytest.raises(PluginCapabilityError, match="peer"):
            mem.caller.call_peer()
        assert mem.dep_caller.call_peer() == "pong"
        with pytest.raises(PluginCapabilityError, match="peer"):
            mem.dep_caller.call_stranger()
        assert mem.trusted_caller.call_stranger() == "hi"

    def test_proxy_unwrap_not_public(self, mem: Memory) -> None:
        from pdm_memory import PluginCapabilityError, PluginMemoryProxy
        from pdm_memory.plugins.proxy import as_memory

        class Tiny(BasePDMPlugin):
            name = "tiny"

        mem.use(Tiny())
        assert not hasattr(PluginMemoryProxy, "unwrap")
        with pytest.raises(PluginCapabilityError):
            _ = mem.tiny.mem.unwrap()  # type: ignore[attr-defined]
        assert as_memory(mem.tiny.mem) is mem

    def test_unknown_capability_fails_fast(self, mem: Memory) -> None:
        class Bad(BasePDMPlugin):
            name = "bad_caps"
            capabilities = frozenset({"read", "nuke_from_orbit"})

        with pytest.raises(ValueError, match="Unknown plugin capabilities"):
            mem.use(Bad())

    def test_hook_priority_orders_plugins(self, mem: Memory) -> None:
        order: list[str] = []

        class GuardDog(BasePDMPlugin):
            name = "guard_dog"
            priority = 10

            def __init__(self) -> None:
                super().__init__()
                self.hooks = {"pre_save": self._hook}

            def _hook(self, sig):
                order.append("guard")
                return sig

        class Auditor(BasePDMPlugin):
            name = "auditor"
            priority = 100

            def __init__(self) -> None:
                super().__init__()
                self.hooks = {"pre_save": self._hook}

            def _hook(self, sig):
                order.append("auditor")
                return sig

        # Install Auditor first — priority must still run GuardDog earlier.
        mem.use(Auditor()).use(GuardDog())
        mem.save("ordered fact", tags=["alpha", "beta", "gamma"])
        assert order == ["guard", "auditor"]

    def test_add_hook_priority_kwarg(self, mem: Memory) -> None:
        order: list[str] = []

        def late(sig):
            order.append("late")
            return sig

        def early(sig):
            order.append("early")
            return sig

        mem.add_hook("pre_save", late, priority=200)
        mem.add_hook("pre_save", early, priority=10)
        mem.save("prio fact", tags=["alpha", "beta", "gamma"])
        assert order == ["early", "late"]
