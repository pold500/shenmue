#pragma once

#include "s1/enhancements/qte_select.hpp"

// ImGui notebook editor for the QTE debug panel: every notebook line is driven by a story flag,
// and the window toggles those flags.
#if SHENMUE_ENHANCEMENT_S1_QTE_SELECT && SHENMUE_IS_S1 && SHENMUE_IS_PC && defined(_WIN32)

#include <cstdint>

namespace shenmue::s1::enhancements {

// Sets or clears a story flag the way Script_scenUtils command 12 does: a newly set flag also
// updates its notebook entry, and the inventory item group is refreshed either way.
void SetStoryFlag(std::uint16_t flag, bool value);

// Draws the notebook editor window (inside an ImGui frame).
void DrawNotebookEditor();

} // namespace shenmue::s1::enhancements

#endif
