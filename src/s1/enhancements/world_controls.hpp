#pragma once

#include "s1/enhancements/qte_select.hpp"

// ImGui time and weather controls for the QTE debug panel.
#if SHENMUE_ENHANCEMENT_S1_QTE_SELECT && SHENMUE_IS_S1 && SHENMUE_IS_PC && defined(_WIN32)

namespace shenmue::s1::enhancements {

// Game thread, once per frame: applies pending time and weather changes.
void WorldControls_Update();
// An area finished loading: its weather system starts without overrides, so re-apply the chosen weather.
void WorldControls_OnMapScriptLoaded();
// Draws the time and weather window (inside an ImGui frame).
void DrawWorldControls();

} // namespace shenmue::s1::enhancements

#endif
