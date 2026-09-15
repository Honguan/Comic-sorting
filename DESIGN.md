# Comic sorting desktop style

Reference: [Linear analysis in awesome-design-md](https://github.com/VoltAgent/awesome-design-md/blob/main/design-md/linear.app/DESIGN.md).
Adapt its restrained lavender-blue accent, thin borders and clear surface hierarchy to a light, data-dense desktop interface; this is not a replica of its dark marketing website.

- Canvas: #f5f6f8; data surfaces: #ffffff; text: #20232d; secondary text: #626774.
- Accent: #5e6ad2, reserved for primary actions, progress and focus. Selection: #e9ecff with dark text.
- Use the system UI font and point size with bold section and table headings; preserve multilingual fallback and DPI scaling.
- Thin #d7dbe3 borders, compact controls, generous table row spacing. Avoid gradients and decorative graphics.
- Keep native keyboard focus, disabled states, table sorting, resizing and collapsible groups. Use the shared ttk styles in ui_theme.py for all windows.
- Preserve space for paths, status and error reasons; do not expand controls at the expense of usable tables.
