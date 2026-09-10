import ast
from pathlib import Path
from string import Formatter
import tempfile
import tkinter as tk
import unittest
from unittest import mock

from test_comic_sorting import comic
from queue_worker import run_jobs
from ui_language import LANGUAGES, TRANSLATIONS, set_language, tr


class UILanguageTests(unittest.TestCase):
    def tearDown(self):
        set_language("zh-TW")

    def test_catalog_covers_messages_and_preserves_format_fields(self):
        formatter = Formatter()
        for source, translations in TRANSLATIONS.items():
            fields = [(field, spec, conversion) for _, field, spec, conversion
                      in formatter.parse(source) if field is not None]
            for language in ("en", "ja"):
                with self.subTest(source=source, language=language):
                    self.assertTrue(translations[language])
                    self.assertEqual(fields, [(field, spec, conversion)
                                             for _, field, spec, conversion
                                             in formatter.parse(translations[language])
                                             if field is not None])
        for name in ("Comic sorting.py", "translation_queue.py", "comic_core.py", "queue_worker.py", "bt_settings.py"):
            tree = ast.parse((Path(__file__).parents[1] / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "tr" and isinstance(node.args[0], ast.Constant)):
                    self.assertIn(node.args[0].value, TRANSLATIONS)

    def test_language_setting_and_queue_action_work_after_restart(self):
        for language in LANGUAGES:
            with self.subTest(language=language), tempfile.TemporaryDirectory() as temp:
                settings_file = Path(temp) / "settings.json"
                comic.save_json(settings_file, {"ui_language": language})
                root = tk.Tk()
                root.withdraw()
                try:
                    with mock.patch.object(comic, "settings_path", return_value=settings_file):
                        app = comic.FileAggregatorApp(root)
                        self.assertEqual(root.title(), tr("漫畫整合工具"))
                        self.assertEqual(app.ui_language.get(), LANGUAGES[language])
                        chapter = Path(temp) / "Chapter 1"
                        work = chapter / "mask"
                        work.mkdir(parents=True)
                        (work / "1.png").touch()
                        app.translation_queue.add_paths([chapter], action="cleanup")
                        run_jobs(app.translation_queue.jobs, app.translation_queue.settings(), "", True,
                                 app.translation_queue.stop, app.translation_queue.events.put)
                        self.assertFalse((work / "1.png").exists())
                        app.ui_language.set(LANGUAGES["ja"])
                        self.assertTrue(app.save_settings())
                        self.assertEqual(comic.load_json(settings_file, {})["ui_language"], "ja")
                        # Choosing a language must not alter running queue action comparisons.
                        self.assertEqual(root.title(), tr("漫畫整合工具"))
                        root.update_idletasks()
                finally:
                    root.destroy()

    def test_unknown_language_uses_traditional_chinese(self):
        set_language("unknown")
        self.assertEqual(tr("漫畫整合工具"), "漫畫整合工具")
