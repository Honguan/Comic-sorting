"""Read settings metadata using the selected BallonsTranslator Python environment.

This script runs in a child process; it never starts the application or models.
"""
import contextlib
from dataclasses import fields, is_dataclass
import io
import json
from pathlib import Path
import sys
from typing import get_type_hints


def describe(value):
    if isinstance(value, dict):
        return {"type": "object", "children": {k: describe(v) for k, v in value.items()}}
    if isinstance(value, list):
        return {"type": "array", "item": describe(value[0]) if value else {}}
    return {"type": {bool: "boolean", int: "integer", float: "number", str: "string",
                     type(None): "any"}[type(value)]}


def declared_types(instance, node):
    """Numeric defaults can be integers even when the native field accepts floats."""
    hints = get_type_hints(type(instance))
    for field in fields(instance):
        child = node.get("children", {}).get(field.name)
        if child is None:
            continue
        value = getattr(instance, field.name)
        annotation = hints.get(field.name)
        if annotation is float:
            child["type"] = "number"
        elif annotation is int:
            child["type"] = "integer"
        elif value is None:
            child["nullable"] = True
        if is_dataclass(value) and child.get("type") == "object":
            declared_types(value, child)


def metadata():
    from ballontranslator.utils import config, fontformat, llm_profiles, text_effects
    from ballontranslator.modules.lazy_registry import iter_lazy_module_specs

    # Use the native serializer: FontFormat's persisted shape differs from asdict().
    program = config.ProgramConfig()
    defaults = json.loads(config.json_dump_program_config(program))
    schema = describe(defaults)
    declared_types(program, schema)
    module = defaults["module"]
    fields = schema["children"]["module"]["children"]
    catalogs = {name: [] for name in ("translator", "ocr", "textdetector", "inpainter")}
    languages = set()
    translator_languages = {}
    for spec in iter_lazy_module_specs():
        catalogs[spec.module_type].append(spec.key)
        languages.update(spec.supported_src_list or [])
        languages.update(spec.supported_tgt_list or [])
        if spec.module_type == "translator":
            translator_languages[spec.key] = {"translate_source": spec.supported_src_list or [],
                                              "translate_target": spec.supported_tgt_list or []}
        params = {}
        param_schema = {}
        for key, info in (spec.params or {}).items():
            if key == "description" or key.startswith("__"):
                continue
            value = info.get("value") if isinstance(info, dict) and "value" in info else info
            params[key] = value
            node = describe(value)
            if isinstance(info, dict) and "value" in info:
                for source, target in (("options", "choices"), ("display_name", "title"),
                                       ("description", "description")):
                    if source in info:
                        node[target] = info[source]
                if info.get("type") == "checkbox":
                    node["type"] = "boolean"
            param_schema[key] = node
        module[spec.module_type + "_params"][spec.key] = params
        fields[spec.module_type + "_params"]["children"][spec.key] = {
            "type": "object", "children": param_schema}
    for name, choices in catalogs.items():
        fields[name]["choices"] = sorted(set(choices))
    for name in ("translate_source", "translate_target"):
        fields[name]["choices"] = sorted(languages)
    for name, enum in (("translate_context", config.TranslateContext),
                       ("llm_translate_context", config.LLMTranslateContext),
                       ("llm_glossary_mode", config.LLMGlossaryMode),
                       ("ocr_text_postprocess", config.OCRTextPostprocess)):
        fields[name]["choices"] = list(enum.Valid)
        fields[name]["strict_choices"] = True
    fields["llm_prior_context_token_budget"]["minimum"] = 1
    schema["children"]["let_letter_case"]["choices"] = list(config.OCRTextPostprocess.Valid)
    schema["children"]["run_pipeline_mode"]["choices"] = ["pipeline", "rendering"]
    schema["children"]["auto_tate_chu_yoko"]["children"]["max_length"].update(minimum=1, maximum=99)
    schema["children"]["imgsave_quality"].update(minimum=0, maximum=100)
    for name in set(fontformat._NEUTRAL_LEGACY_EFFECT_FIELDS) | set(fontformat._LEGACY_EFFECT_VIEW_NAMES):
        node = schema["children"]["global_fontformat"]["children"].get(name)
        if node is not None:
            node["read_only"] = True

    profile = llm_profiles.profile_to_dict(llm_profiles.LLMProfile())
    fields["llm_profiles"]["item"] = describe(profile)
    fields["llm_profiles"]["item"]["children"]["api_key"] = {"type": "secret"}
    templates = {"llm_profiles": {"LLM": profile}}
    replacement = {"keyword": "", "sub": "", "use_reg": False, "case_sens": True}
    for key in ("ocr_sublist", "pre_mt_sublist", "mt_sublist"):
        templates[key] = {"Replacement": replacement}
        schema["children"][key]["item"] = describe(replacement)
    templates["text_transform"] = {
        kind: json.loads(json.dumps(fontformat.asdict(cls())))
        for kind, cls in (("projective", fontformat.ProjectiveTextTransform),
                          ("bend", fontformat.BendTextTransform),
                          ("sine", fontformat.SineTextTransform),
                          ("grid", fontformat.GridTextTransform))
    }
    templates["effects"] = {
        kind: cls().to_serializable_dict()
        for kind, cls in (("stroke", text_effects.StrokeEffect),
                          ("shadow", text_effects.ShadowEffect),
                          ("glow", text_effects.GlowEffect),
                          ("hollow", text_effects.HollowEffect),
                          ("text_fill", text_effects.TextFillEffect),
                          ("image", text_effects.ImageEffect),
                          ("synthetic_bold", text_effects.SyntheticBoldEffect))
    }
    from ballontranslator.ui.text_engine.effects.filters import get_filter_registry
    for spec in get_filter_registry().specs:
        templates["effects"][spec.filter_id] = text_effects.FilterEffect(
            spec.filter_id, params=spec.default_params()).to_serializable_dict()
    templates["paint"] = {
        kind: cls().to_serializable_dict()
        for kind, cls in (("solid", text_effects.SolidPaint),
                          ("linear_gradient", text_effects.LinearGradientPaint),
                          ("texture", text_effects.TexturePaint))
    }
    return {"defaults": defaults, "schema": schema, "templates": templates,
            "translator_languages": translator_languages}


def prepare(data):
    from ballontranslator.utils.config import ProgramConfig
    from ballontranslator.utils.fontformat import coerce_text_transform
    from ballontranslator.utils.secret_store import SecretStore

    module = data.get("module", {})
    for transform in data.get("global_fontformat", {}).get("text_transform", []):
        coerce_text_transform(transform)
    for profile in module.get("llm_profiles", []):
        # Preserve existing secret objects, and use native storage for replacements.
        if "api_key" in profile:
            profile["api_key"] = SecretStore().prepare_for_save(profile.get("id", ""), profile["api_key"])
    stage_bits = {"enable_detect": 1, "enable_ocr": 2, "enable_inpaint": 4, "enable_translate": 8}
    if all(key in module for key in stage_bits):
        module["finish_code"] = sum(bit for key, bit in stage_bits.items() if module[key])
    # Validate with native constructors, but return the original mapping so unknown
    # fields cannot be silently discarded by BallonsTranslator's dataclass loader.
    ProgramConfig(**json.loads(json.dumps(data)))
    return data


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.path.insert(0, str(Path.cwd()))
    request = json.load(sys.stdin)
    # Native imports may log. Neither a secret nor config values may enter stdout logs.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            result = metadata() if request["operation"] == "metadata" else prepare(request["data"])
        except Exception as error:
            result = {"bridge_error": type(error).__name__}
    sys.stdout.write(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
