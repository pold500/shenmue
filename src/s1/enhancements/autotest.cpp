#include "s1/enhancements/autotest.hpp"

#if SHENMUE_ENHANCEMENT_S1_QTE_SELECT && SHENMUE_IS_S1 && SHENMUE_IS_PC && defined(_WIN32)

#include "s1/d3t/renderer.hpp"
#include "s1/task/scen.hpp"
#include "s1/ui_runtime.hpp"
#include "s1/window.hpp"

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <optional>
#include <string>
#include <vector>

namespace shenmue::s1::enhancements {
namespace {

constexpr const char *kEnvironmentVariable = "SHEN_QTE_AUTOTEST";
constexpr const char *kAllPresets = "all";
constexpr const char *kReportFile = "qte_autotest.txt";

// Frames to wait on the title screen before the first launch (archives, UI and panel ready).
constexpr std::int32_t kStartDelayFrames = 180;
// Frames a launch may take to reach the area before it is reported as stuck.
constexpr std::int32_t kLoadTimeoutFrames = 60 * 90;
// Frames the scene is watched once the area has loaded.
constexpr std::int32_t kObserveFrames = 60 * 90;
// Frames the scene keeps running after the first QTE prompt (the prompt frame is dumped), then the next preset starts.
constexpr std::int32_t kAfterQteFrames = 60 * 2;
// When the two frame dumps are taken, counted from the area load.
constexpr std::int32_t kEarlyDumpFrame = 60 * 8;
constexpr std::int32_t kLateDumpFrame = 60 * 45;
// The scene counts as started when the player lost control for at least this many frames.
constexpr std::int32_t kControlTakenThreshold = 60;
// Frames between the end of the report and quitting (lets a test runner unmute the game first).
constexpr std::int32_t kQuitDelayFrames = 60 * 5;

enum class Stage : std::uint8_t { Disabled, Starting, Launching, Observing, Finished };

struct Observation {
    std::int32_t sequenceFrames = 0;
    std::int32_t scriptFrames = 0;
    std::int32_t controlTakenFrames = 0;
    std::int32_t qteFrames = 0;
    // Observe frame of the first QTE prompt.
    std::int32_t qteStartFrame = 0;
};

Stage g_stage = Stage::Disabled;
std::vector<std::size_t> g_queue;
std::size_t g_current = 0;
std::int32_t g_frames = 0;
Observation g_observation;
std::FILE *g_report = nullptr;

std::vector<std::size_t> ParseSelection(const char *value) {
    std::vector<std::size_t> presets;
    if (std::strcmp(value, kAllPresets) == 0 || !*value) {
        for (std::size_t index = 0; index < QteSelect_PresetCount(); ++index)
            presets.push_back(index);
        return presets;
    }
    for (const char *cursor = value; *cursor;) {
        char *end = nullptr;
        const unsigned long index = std::strtoul(cursor, &end, 10);
        if (end == cursor)
            break;
        if (index < QteSelect_PresetCount())
            presets.push_back(index);
        cursor = *end == ',' ? end + 1 : end;
    }
    return presets;
}

void Report(const char *format, const char *label, const Observation &o, const char *verdict) {
    if (!g_report)
        return;
    std::fprintf(g_report, format, g_queue[g_current], label, verdict, o.sequenceFrames, o.scriptFrames, o.controlTakenFrames, o.qteFrames);
    std::fflush(g_report);
}

void DumpFrame(const char *suffix) {
    std::string name = "qte_autotest_" + std::to_string(g_queue[g_current]) + suffix + ".png";
    d3t_DumpFramePng(name.c_str());
}

void StartNext() {
    g_observation = {};
    g_frames = 0;
    if (g_current >= g_queue.size()) {
        g_stage = Stage::Finished;
        if (g_report) {
            std::fputs("done\n", g_report);
            std::fclose(g_report);
            g_report = nullptr;
        }
        return;
    }
    QteSelect_LaunchPreset(g_queue[g_current]);
    g_stage = Stage::Launching;
}

void Finish(const char *verdict) {
    constexpr const char *kLine = "[%zu] %s: %s (sequence %d, scripted scene %d, control taken %d, qte %d frames)\n";
    Report(kLine, QteSelect_PresetLabel(g_queue[g_current]), g_observation, verdict);
    ++g_current;
    StartNext();
}

} // namespace

void Autotest_Update() {
    switch (g_stage) {
    case Stage::Disabled: {
        static bool checked = false;
        if (checked)
            return;
        checked = true;
        const char *const value = std::getenv(kEnvironmentVariable);
        if (!value)
            return;
        g_queue = ParseSelection(value);
        g_report = std::fopen(kReportFile, "w");
        g_stage = Stage::Starting;
        return;
    }
    case Stage::Starting:
        if (++g_frames >= kStartDelayFrames)
            StartNext();
        return;
    case Stage::Launching:
        if (!QteSelect_IsLaunchPending() && QteSelect_InGame()) {
            g_stage = Stage::Observing;
            g_frames = 0;
        } else if (++g_frames >= kLoadTimeoutFrames) {
            Finish("FAILED: area did not load");
        }
        return;
    case Stage::Observing: {
        Observation &o = g_observation;
        if (g_pSCNC && g_pSCNC->header.active)
            ++o.sequenceFrames;
        if (g_scenarioFunctionWork && g_scenarioFunctionWork->active)
            ++o.scriptFrames;
        if (!SCEN_IsPlayerAllowed())
            ++o.controlTakenFrames;
        if (g_sfxOverlayState != UISfxOverlayState::Idle) {
            if (++o.qteFrames == 1) {
                DumpFrame("_qte");
                o.qteStartFrame = g_frames;
            }
        }
        ++g_frames;
        if (g_frames == kEarlyDumpFrame)
            DumpFrame("_a");
        if (g_frames == kLateDumpFrame)
            DumpFrame("_b");
        const bool qteDone = o.qteFrames > 0 && g_frames - o.qteStartFrame >= kAfterQteFrames;
        if (g_frames >= kObserveFrames || qteDone) {
            // The scripted-scene flag stays set while roaming in many areas, so it is reported but not trusted.
            const bool started = o.sequenceFrames > 0 || o.controlTakenFrames >= kControlTakenThreshold;
            Finish(o.qteFrames > 0 ? "QTE reached" : started ? "scene started, no QTE yet" : "NOT STARTED");
        }
        return;
    }
    case Stage::Finished:
        if (++g_frames == kQuitDelayFrames)
            g_shouldQuit = true;
        return;
    }
}

} // namespace shenmue::s1::enhancements

#endif
