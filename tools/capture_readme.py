"""Capture actual AppKit components with staged states, not a live session.

Run on macOS: python -m tools.capture_readme
No audio/MIDI device or personal settings are used.
"""
from pathlib import Path


def main():
    from AppKit import (
        NSApplication, NSWindow, NSWindowStyleMaskTitled, NSBackingStoreBuffered,
        NSBitmapImageFileTypePNG, NSImage, NSColor, NSRectFill, NSBitmapImageRep,
    )
    from justpiano import keyboardview
    from justpiano.keyboard import KeyboardController

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(0)
    controller = KeyboardController()
    panel = keyboardview.KeyboardPanel(controller, None, keyboardview._build_classes())
    view = panel._view_controller.view()
    frame = view.frame()
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        frame, NSWindowStyleMaskTitled, NSBackingStoreBuffered, False)
    window.setTitle_("Just Piano — documentation capture")
    window.setContentView_(view)
    output = Path(__file__).resolve().parents[1] / "docs" / "screenshots"
    output.mkdir(parents=True, exist_ok=True)
    states = [
        ("keyboard", "Grand Piano · A–; to play · click lower for louder notes", (60, 64, 67), False),
        ("recording", "Felt Piano · Recording 0:12", (57, 60, 64, 69), False),
        ("muted", "Rhodes (electric) · Muted · recording stays active", (), True),
    ]
    for name, status, notes, muted in states:
        controller.status = status
        controller.set_muted(muted)
        controller.lights.release_all()
        for note in notes:
            controller.lights.press(note, 88)
        controller.refresh()
        panel.refresh_status()
        panel.refresh_mute()
        view.display()
        representation = view.bitmapImageRepForCachingDisplayInRect_(view.bounds())
        view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), representation)
        image = NSImage.alloc().initWithSize_(frame.size)
        image.lockFocus()
        NSColor.windowBackgroundColor().setFill()
        NSRectFill(view.bounds())
        representation.drawInRect_(view.bounds())
        image.unlockFocus()
        rendered = NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
        data = rendered.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})
        path = output / f"{name}.png"
        if not data.writeToFile_atomically_(str(path), True):
            raise OSError(f"Could not write {path}")
        print(path)


if __name__ == "__main__":
    main()
