#pragma once

#include "shenmue.h"

#ifndef SHENMUE_ENHANCEMENT_S1_QTE_SELECT
#define SHENMUE_ENHANCEMENT_S1_QTE_SELECT 0
#endif

// Always-open Dear ImGui debug panel for Shenmue I:
//  * starts gameplay right before a story QTE scene: the game soft-resets into a fresh new game,
//    the chosen story preset (flags with their notebook entries, scenario step, time, optional
//    teleport) is applied, and the scene's area loads so its own scripts start the event the way
//    they would in a normal playthrough;
//  * overrides the weather (clear / overcast / rain / snow, or back to the calendar data);
//  * skips the boot splash screens.
#if SHENMUE_ENHANCEMENT_S1_QTE_SELECT && SHENMUE_IS_S1 && SHENMUE_IS_PC && defined(_WIN32)

#include <cstdint>

namespace shenmue::s1::enhancements {

// HLib_mainModule, once per frame: performs a requested soft reset.
void QteSelect_Update();
// PresentLatestFrame, right before the swap chain presents: builds and draws the panel.
void QteSelect_Render();
// Audio_Module, right after the new-game defaults when a map launch is active: applies the preset.
void QteSelect_ApplyPendingPreset();
// SCEN_LoadCurrentMapScript, once an area's scripts have started: arms the preset's teleport.
void QteSelect_OnMapScriptLoaded();
// Window procedure. Returns true when the panel consumed the message and the game must not see it.
bool QteSelect_HandleWindowMessage(void *window, std::uint32_t message, std::uint64_t wParam, std::int64_t lParam);

} // namespace shenmue::s1::enhancements

#endif
