"""dc-qalink live assets (0.2, docs/ASSETS.md): converters from PNG/JPEG, OBJ and glTF to the Dreamcast's own forms,
the DQA1 file the console viewer loads, a reference PC render on the shared camera, and the difference view.

Needs numpy (VQ encoding, palettes, the reference render). Pillow is optional: JPEG and resizing use it when it is
installed; PNG is read with the standard library otherwise. No game formats here: game decoders are private plugins."""
VERSION = '0.2.0'
