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

#include <cstddef>
#include <cstdint>

namespace shenmue::s1::enhancements {

// HLib_mainModule, once per frame: performs a requested soft reset.
void QteSelect_Update();
// HLib_mainModule, right after the input devices were read: injects scripted button presses.
void QteSelect_AfterInputUpdate();
// PresentLatestFrame, right before the swap chain presents: builds and draws the panel.
void QteSelect_Render();
// Audio_Module, right after the new-game defaults when a map launch is active: applies the preset.
void QteSelect_ApplyPendingPreset();
// SCEN_LoadCurrentMapScript, once an area's scripts have started: arms the preset's teleport.
void QteSelect_OnMapScriptLoaded();
// Window procedure. Returns true when the panel consumed the message and the game must not see it.
bool QteSelect_HandleWindowMessage(void *window, std::uint32_t message, std::uint64_t wParam, std::int64_t lParam);
// Moves Ryo to an area entry point: a regular area transition while in game (story state kept),
// a soft reset into a new game from the title screen.
void RequestLocationJump(std::int32_t sceneId, std::uint32_t areaId, std::uint32_t entryPoint);

// Scene presets, for the autotest bot.
std::size_t QteSelect_PresetCount();
const char *QteSelect_PresetLabel(std::size_t index);
void QteSelect_LaunchPreset(std::size_t index);
// A launch (reset, preset or teleport) is still in progress.
bool QteSelect_IsLaunchPending();
// The game is in an area (not the title screen, not loading).
bool QteSelect_InGame();

} // namespace shenmue::s1::enhancements

#endif
