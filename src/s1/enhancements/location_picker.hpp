#pragma once

#include "s1/enhancements/qte_select.hpp"

// ImGui location picker for the QTE debug panel: every area of the game (scene/NN/AREA/MAPINFO.BIN
// in the archives) with the entry points its MAPE chunk lists.
#if SHENMUE_ENHANCEMENT_S1_QTE_SELECT && SHENMUE_IS_S1 && SHENMUE_IS_PC && defined(_WIN32)

namespace shenmue::s1::enhancements {

// Draws the location picker window (inside an ImGui frame).
void DrawLocationPicker();

} // namespace shenmue::s1::enhancements

#endif
