# OmniParser v2 Icon Caption Model

This directory vendors the fine-tuned Florence-2 icon-caption model from
`microsoft/OmniParser-v2.0` together with the Florence-2 processor files needed
for offline loading.

- Model source: https://huggingface.co/microsoft/OmniParser-v2.0
- Processor source: https://huggingface.co/microsoft/Florence-2-base
- Icon-caption model license: MIT (`LICENSE`)

The `auto_map` entries in `config.json` point to the vendored local Python
modules so packaged releases do not need to fetch model code at runtime.
Exact source revisions and key-file SHA-256 values are recorded in
`model_manifest.json`.
