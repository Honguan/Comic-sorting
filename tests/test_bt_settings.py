from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest import mock

from bt_config_bridge import describe
from bt_settings import ConfigDocument, ConfigEditor, merge_defaults, parse_json


def metadata():
    profile = dict(id="demo", name="Demo", api_key="", model="model-a", model_options=["model-a", "model-b"],
                   max_tokens=8192, temperature=0.1, prompt="translate", support_text=True,
                   support_vision=True, support_image=False, built_in=False)
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
        self.assertFalse(self.path.with_suffix(".json.tmp").exists())

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

    def select(self, path):
        item = next(i for i, p in self.editor.paths.items() if p == path)
        self.editor.tree.selection_set(item)
        self.editor.select()
        self.root.update()

    def entry(self):
        return next(w for w in self.editor.panel.winfo_children() if isinstance(w, (tk.ttk.Entry, tk.ttk.Combobox)))

    def test_all_fields_are_reachable_and_secret_is_masked(self):
        from bt_settings import secret_path
        def paths(value, path=()):
            yield path
            children = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else ()
            if not secret_path(path):
                for key, child in children:
                    yield from paths(child, path + (key,))
        self.assertEqual(set(self.editor.paths.values()), set(paths(self.editor.document.data)))
        displayed = str([self.editor.tree.item(i) for i in self.editor.paths])
        self.assertNotIn("opaque", displayed)
        self.select(("module", "llm_profiles", 0, "api_key"))
        self.assertEqual(self.entry().get(), "")
        self.assertTrue(self.entry().cget("show"))

    def test_edit_save_reload_and_secret_retention(self):
        self.select(("module", "llm_profiles", 0, "temperature"))
        self.entry().delete(0, "end")
        self.entry().insert(0, "0.3")
        self.select(("module", "llm_profiles", 0, "api_key"))
        self.editor.save()
        self.wait_idle()
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["module"]["llm_profiles"][0]["temperature"], .3)
        self.assertEqual(saved["module"]["llm_profiles"][0]["api_key"], self.raw["module"]["llm_profiles"][0]["api_key"])
        self.editor.reload()
        self.wait_idle()
        self.assertEqual(self.editor.document.data, saved)

    def test_search_and_prompt_unicode_newlines(self):
        self.editor.search.set("溫度")
        self.editor.rebuild()
        self.assertIn(("module", "llm_profiles", 0, "temperature"), self.editor.paths.values())
        self.editor.clear_search()
        self.select(("module", "llm_profiles", 0, "prompt"))
        widget = next(w for w in self.editor.panel.winfo_children() if isinstance(w, tk.Text))
        widget.delete("1.0", "end")
        widget.insert("1.0", "繁體中文\n日本語\n한국어")
        self.editor.save()
        self.wait_idle()
        self.assertEqual(self.editor.document.data["module"]["llm_profiles"][0]["prompt"], "繁體中文\n日本語\n한국어")

    def test_profile_add_duplicate_reorder_remove(self):
        self.select(("module", "llm_profiles"))
        self.editor.add()
        self.root.update()
        profiles = self.editor.document.data["module"]["llm_profiles"]
        self.assertEqual(len(profiles), 2)
        self.editor.duplicate()
        self.root.update()
        self.assertEqual(len(set(p["id"] for p in profiles)), 3)
        copied_id = profiles[-1]["id"]
        self.editor.move(-1)
        self.root.update()
        self.assertEqual(profiles[1]["id"], copied_id)
        with mock.patch("bt_settings.messagebox.askyesno", return_value=True):
            self.editor.remove()
        self.assertEqual(len(profiles), 2)

    def test_invalid_field_blocks_save_and_navigation(self):
        self.select(("imgsave_quality",))
        self.entry().delete(0, "end")
        self.entry().insert(0, "not a number")
        with mock.patch("bt_settings.messagebox.showerror") as error:
            self.editor.save()
            self.assertTrue(error.called)
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_invalid_draft_can_be_discarded_without_touching_disk(self):
        self.select(("imgsave_quality",))
        self.entry().delete(0, "end")
        self.entry().insert(0, "invalid")
        with mock.patch("bt_settings.messagebox.askyesno", return_value=True):
            self.editor.reload()
        self.wait_idle()
        self.assertEqual(self.editor.document.data["imgsave_quality"], 100)
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_closing_can_cancel_save_or_discard(self):
        self.select(("imgsave_quality",))
        self.entry().delete(0, "end")
        self.entry().insert(0, "90")
        with mock.patch("bt_settings.messagebox.askyesnocancel", return_value=None):
            self.editor.close()
        self.assertTrue(self.editor.winfo_exists())
        with mock.patch("bt_settings.messagebox.askyesnocancel", return_value=False):
            self.editor.close()
        self.assertFalse(self.editor.winfo_exists())
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_footer_stays_usable_at_minimum_size(self):
        self.root.deiconify()
        self.editor.geometry("760x540")
        self.editor.status.set("Long backup path " * 40)
        self.root.update()
        self.assertTrue(self.editor.save_button.winfo_ismapped())
        self.assertGreaterEqual(self.editor.save_button.winfo_width(), self.editor.save_button.winfo_reqwidth())
        self.assertLessEqual(self.editor.save_button.winfo_rootx() + self.editor.save_button.winfo_width(),
                             self.editor.winfo_rootx() + self.editor.winfo_width())


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
