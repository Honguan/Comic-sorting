from copy import deepcopy
import json
from pathlib import Path
import tempfile
import sys
import time
import tkinter as tk
import unittest
from unittest import mock

from bt_config_bridge import describe
from bt_settings import ConfigDocument, ConfigEditor, bridge, merge_defaults, parse_json


def metadata():
    profile = dict(id="demo", name="Demo", api_key="", model="model-a", model_options=["model-a", "model-b"],
                   max_tokens=8192, temperature=0.1, prompt="translate", support_text=True,
                   support_vision=True, support_image=False, built_in=False, require_api_key=True)
    defaults = dict(module=dict(enable_translate=True, translator_llm_id="demo", ocr_llm_id="demo", inpaint_llm_id="",
                                llm_profiles=[profile], translator_params={"LLM": {"delay": 0.3}}),
                    imgsave_quality=100, mirrors={"huggingface": None}, ocr_sublist=[])
    schema = describe(defaults)
    schema["children"]["module"]["children"]["llm_profiles"]["item"]["children"]["api_key"] = {"type": "secret"}
    schema["children"]["mirrors"]["children"]["huggingface"]["nullable"] = True
    return dict(defaults=defaults, schema=schema, templates={"llm_profiles": {"LLM": profile}})


class ConfigFixture:
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "設定.json"
        self.meta = metadata()
        self.raw = deepcopy(self.meta["defaults"])
        self.raw["custom"] = {"future": ["日本語", True, 1.5, None]}
        self.raw["module"]["llm_profiles"][0]["api_key"] = {"storage": "portable_obfuscated", "version": 1, "value": "opaque"}
        self.path.write_text(json.dumps(self.raw, ensure_ascii=False), encoding="utf-8")
        self.before = self.path.read_bytes()

    def document(self):
        return ConfigDocument(self.path, self.meta)


class ConfigDocumentTests(ConfigFixture, unittest.TestCase):

    def test_bridge_does_not_import_modules_from_exe_bundle(self):
        with tempfile.TemporaryDirectory() as bundle:
            folder = Path(bundle)
            (folder / 'socket.py').write_text("raise ImportError('bundled Python version mismatch')\n")
            (folder / 'bt_config_bridge.py').write_text(
                "import socket, json, sys\njson.load(sys.stdin)\nprint(json.dumps({'loaded': True}))\n")
            with mock.patch('bt_settings.translator_command', return_value=([sys.executable], self.path.parent, None)), \
                    mock.patch.object(sys, '_MEIPASS', bundle, create=True):
                self.assertEqual(bridge({'bt_path': '', 'bt_config': ''}, 'metadata'), {'loaded': True})

    def test_missing_fields_and_unknown_values_survive_round_trip(self):
        del self.raw["module"]["llm_profiles"][0]["temperature"]
        self.raw["module"]["translator_params"] = {"Plugin": {"future": {"nested": []}}}
        self.path.write_text(json.dumps(self.raw, ensure_ascii=False), encoding="utf-8")
        before = self.path.read_bytes()
        doc = self.document()
        self.assertEqual(doc.data["module"]["llm_profiles"][0]["temperature"], 0.1)
        self.assertIn("LLM", doc.data["module"]["translator_params"])
        doc.set(("imgsave_quality",), 95)
        backup = doc.save(lambda data: data)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["custom"], self.raw["custom"])
        self.assertEqual(saved["module"]["translator_params"]["Plugin"], self.raw["module"]["translator_params"]["Plugin"])
        self.assertEqual(saved["module"]["llm_profiles"][0]["api_key"], self.raw["module"]["llm_profiles"][0]["api_key"])
        self.assertEqual(backup.read_bytes(), before)
        self.assertFalse(doc.dirty)

    def test_untouched_document_does_not_rewrite_or_backup(self):
        self.assertIsNone(self.document().save(mock.Mock(side_effect=AssertionError)))
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(list(self.path.parent.glob("*.bak")), [])

    def test_external_edits_before_and_during_validation_are_preserved(self):
        doc = self.document()
        doc.set(("imgsave_quality",), 80)
        changed = b'{"changed":true}'
        def prepare(data):
            self.path.write_bytes(changed)
            return data
        with self.assertRaises(ValueError):
            doc.save(prepare)
        with self.assertRaises(ValueError):
            doc.save(lambda data: data)
        self.assertEqual(self.path.read_bytes(), changed)
        self.assertEqual(list(self.path.parent.glob("*.bak")), [])

    def test_validation_failure_leaves_original_and_draft_intact(self):
        doc = self.document()
        doc.set(("imgsave_quality",), 80)
        with self.assertRaises(ValueError):
            doc.save(mock.Mock(side_effect=ValueError("native rejected")))
        self.assertTrue(doc.dirty)
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_atomic_write_failure_retains_original_and_backup(self):
        doc = self.document()
        doc.set(("imgsave_quality",), 80)
        with mock.patch("comic_core.os.replace", side_effect=OSError("write failed")), self.assertRaises(OSError):
            doc.save(lambda data: data)
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(next(self.path.parent.glob("*.bak")).read_bytes(), self.before)
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_types_non_finite_and_duplicate_json_keys_rejected(self):
        doc = self.document()
        for path, value in ((("module", "enable_translate"), 1), (("imgsave_quality",), "95"),
                            (("module", "llm_profiles", 0, "temperature"), float("nan"))):
            with self.subTest(path=path), self.assertRaises(ValueError):
                doc.set(path, value)
        for content in ('{"a":1,"a":2}', '{"value":NaN}'):
            with self.assertRaises(ValueError):
                parse_json(content)

    def test_array_and_profile_reference_validation(self):
        doc = self.document()
        doc.data["module"]["llm_profiles"].append({"id": "demo"})
        with self.assertRaises(ValueError):
            doc.save(lambda data: data)
        doc = self.document()
        doc.data["module"]["llm_profiles"] = []
        with self.assertRaises(ValueError):
            doc.save(lambda data: data)
        doc = self.document()
        doc.data["module"]["llm_profiles"].append("invalid")
        with self.assertRaises(ValueError):
            doc.save(lambda data: data)

    def test_dynamic_model_and_profile_options(self):
        doc = self.document()
        self.assertEqual(doc.choices(("module", "llm_profiles", 0, "model")), ["model-a", "model-b"])
        self.assertEqual(doc.choices(("module", "translator_llm_id")), ["demo"])
        self.assertEqual(doc.choices(("module", "inpaint_llm_id")), [])
        doc.set(("module", "llm_profiles", 0, "model"), "new-model")
        self.assertIn("new-model", doc.choices(("module", "llm_profiles", 0, "model")))


class ConfigEditorTests(ConfigFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.bridge = mock.patch("bt_settings.bridge", side_effect=lambda settings, operation, data=None: self.meta if operation == "metadata" else data)
        self.bridge.start()
        self.addCleanup(self.bridge.stop)
        self.editor = ConfigEditor(self.root, {"bt_config": str(self.path)})
        self.wait_idle()

    def wait_idle(self):
        limit = time.monotonic() + 3
        while self.editor.working and time.monotonic() < limit:
            self.root.update()
            time.sleep(.01)
        self.root.update()
        self.assertFalse(self.editor.working)

    def test_focused_form_preserves_hidden_settings_and_saves_active_profile(self):
        paths = self.editor.fields
        self.assertNotIn(("imgsave_quality",), paths)
        self.assertNotIn(("custom", "future"), paths)
        profile = ("module", "llm_profiles", 0)
        self.assertNotIn(profile + ("support_text",), paths)
        paths[profile + ("temperature",)][0].set("0.7")
        paths[profile + ("prompt",)][0].set("繁體中文\n日本語")
        self.editor.save()
        self.wait_idle()
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["module"]["llm_profiles"][0]["temperature"], 0.7)
        self.assertEqual(saved["module"]["llm_profiles"][0]["api_key"], self.raw["module"]["llm_profiles"][0]["api_key"])
        self.assertEqual(saved["custom"], self.raw["custom"])
        self.assertFalse(self.editor.has_changes())

    def test_invalid_form_is_atomic_and_close_can_cancel(self):
        path = ("module", "llm_profiles", 0, "temperature")
        self.editor.fields[path][0].set("invalid")
        with mock.patch("bt_settings.messagebox.showerror") as error:
            self.editor.save()
        error.assert_called_once()
        self.assertEqual(self.path.read_bytes(), self.before)
        with mock.patch("bt_settings.messagebox.askyesnocancel", return_value=None):
            self.editor.close()
        self.assertTrue(self.editor.winfo_exists())

    def test_mode_module_and_font_round_trip(self):
        self.meta["defaults"].update(run_pipeline_mode="pipeline", let_family_flag=0,
            global_fontformat=dict(font_family="Arial", font_size=24.0, vertical=False))
        self.meta["defaults"]["module"].update(translator="LLM", translator_params={"LLM": {"delay": .3}, "Other": {"delay": .5}})
        self.meta["schema"] = describe(self.meta["defaults"])
        self.meta["schema"]["children"]["module"]["children"]["llm_profiles"]["item"]["children"]["api_key"] = {"type": "secret"}
        self.editor.loaded(self.document())
        self.assertNotIn(("module", "translator_params", "Other", "delay"), self.editor.fields)
        self.editor.fields[("run_pipeline_mode",)][0].set("僅排版 / 渲染")
        self.editor.fields[("let_family_flag",)][0].set("使用下方全域設定")
        self.editor.fields[("global_fontformat", "font_family")][0].set("Microsoft JhengHei")
        self.editor.fields[("module", "translator")][0].set("Other")
        self.editor.rebuild()
        self.assertIn(("module", "translator_params", "Other", "delay"), self.editor.fields)
        self.editor.save()
        self.wait_idle()
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["run_pipeline_mode"], "rendering")
        self.assertEqual(saved["let_family_flag"], 1)
        self.assertEqual(saved["global_fontformat"]["font_family"], "Microsoft JhengHei")


class EditorIntegrationTests(ConfigFixture, unittest.TestCase):
    def test_queue_and_parent_close_respect_unsaved_editor(self):
        from test_comic_sorting import comic
        from queue_worker import Job
        from ui_language import tr
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        with mock.patch.object(comic, "load_json", return_value={}), mock.patch.object(comic.FileAggregatorApp, "save_settings", return_value=True):
            app = comic.FileAggregatorApp(root)
        worker = app.translation_queue
        worker.config.set(str(self.path))
        with mock.patch.object(worker, "command"), mock.patch("bt_settings.bridge", return_value=self.meta):
            worker.edit_settings()
            editor = worker.config_editor
            limit = time.monotonic() + 3
            while editor.working and time.monotonic() < limit:
                root.update()
                time.sleep(.01)
            self.assertIsNotNone(editor.document)
            worker.jobs = [Job(self.path.parent, "translate")]
            with mock.patch("translation_queue.messagebox.showerror") as error:
                self.assertFalse(worker.validate())
                self.assertEqual(error.call_args.args[1], tr("請先儲存並關閉設定編輯器"))
            with mock.patch("translation_queue.subprocess.Popen") as process:
                worker.open_settings()
                process.assert_not_called()
            editor.document.set(("imgsave_quality",), 80)
            with mock.patch("bt_settings.messagebox.askyesnocancel", return_value=None):
                app.close()
            self.assertTrue(root.winfo_exists())
            self.assertTrue(editor.winfo_exists())


if __name__ == "__main__":
    unittest.main()
