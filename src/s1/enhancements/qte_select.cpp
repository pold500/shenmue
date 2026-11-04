#include "s1/enhancements/qte_select.hpp"

#if SHENMUE_ENHANCEMENT_S1_QTE_SELECT && SHENMUE_IS_S1 && SHENMUE_IS_PC && defined(_WIN32)

#include "common/types.hpp"
#include "d3t/buffer.hpp"
#include "d3t/dx11_renderer.hpp"
#include "s1/boot.hpp"
#include "s1/date_time.hpp"
#include "s1/flags.hpp"
#include "s1/launch_options.hpp"
#include "s1/player_stats.hpp"
#include "s1/collision.hpp"
#include "s1/task/cdtt.hpp"
#include "s1/task/char.hpp"
#include "s1/task/ctrl.hpp"
#include "s1/task/mmtk.hpp"
#include "s1/task/scen.hpp"
#include "s1/task/time.hpp"
#include "s1/task/wexe.hpp"
#include "s1/ui_runtime.hpp"
#include "s1/window.hpp"

#include "imgui.h"
#include "imgui_impl_dx11.h"
#include "imgui_impl_win32.h"

#include <d3d11.h>
#include <dxgi.h>
#include <wrl/client.h>

#include <array>
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <initializer_list>
#include <optional>
#include <span>
#include <string>
#include <system_error>

extern IMGUI_IMPL_API LRESULT ImGui_ImplWin32_WndProcHandler(HWND window, UINT message, WPARAM wParam, LPARAM lParam);

namespace shenmue::s1 {
extern std::array<std::uint8_t, 128> g_shenmueFlags;
}

namespace shenmue::s1::enhancements {
namespace {

using Microsoft::WRL::ComPtr;

// ---------------------------------------------------------------------------------------------
// Story presets. Each one is the state the scene's own scripts check before they start the
// event; the conditions come from the area's .scn scripts (Script_scenUtils flag reads along the
// chain that spawns the QTE).
// ---------------------------------------------------------------------------------------------

struct StoryFlag {
    std::uint16_t id;
    bool value;
};

// A point (in the coordinates character positions use) inside the area the scene's trigger tests.
struct TeleportTarget {
    float x;
    float z;
};

struct StoryPreset {
    const char *label;
    std::int32_t sceneId;
    std::array<char, 4> area;
    std::uint32_t entryPoint;
    // g_currentScenarioStep (act 1 < 110, act 2 110-199, act 3 >= 200); nullopt keeps the default.
    std::optional<std::int16_t> scenarioStep;
    // In-game hour; nullopt keeps the new-game time.
    std::optional<std::uint8_t> hour;
    std::span<const StoryFlag> flags;
    // Where Ryo is moved once the area has loaded; nullopt leaves him at the entry point.
    std::optional<TeleportTarget> teleport;
};

// Unnamed flags in script_catalogue.def, named here by what the scripts use them for.
constexpr std::uint16_t kFlagHarborGoroEventArmed = 887;
constexpr std::uint16_t kFlagHarborJobQteBlockedA = 843;
constexpr std::uint16_t kFlagHarborJobQteBlockedB = 858;
constexpr std::uint16_t kFlagHarborBrotherQteBlocked = 954;

// Area entry point used for every preset: the area's default spawn.
constexpr std::uint32_t kDefaultEntryPoint = 0;
constexpr std::int16_t kActOneStep = 20;
constexpr std::int16_t kActTwoStep = 120;
constexpr std::int16_t kActThreeStep = 200;
constexpr std::uint8_t kNightHour = 20;
// Latest time of the day the clock may run to (Ryo is sent home at 23:30).
constexpr std::uint8_t kDayEndHour = 23;
constexpr std::uint8_t kDayEndMinute = 30;
// Centre of the quay box act02_mfsy.scn waits in before the Hisaka event (fn 0xf0ee:
// x 122.48..126.78, z 14.34..42.44, 8:30-23:29).
constexpr TeleportTarget kHarborHisakaTrigger{124.63f, 28.39f};
// act03_mfsy.scn waits (fn 0xf8a0) for Ryo near a point in the evening: 19:00-23:59 within 13
// of (2, 92) before the job QTE, 19:00-23:00 within 8 of (-95.85, 62.81) before the brother QTE.
constexpr TeleportTarget kHarborJobTrigger{2.0f, 92.0f};
constexpr TeleportTarget kHarborBrotherTrigger{-95.85f, 62.81f};
// act02_d000.scn script zone 17 (fn 0x2500b): within 1.5 of (-71.79, 79.86), 8:30-18:00, then the
// action button starts the Jimmy chase (fn 0x33ab9). The preset starts at the Dobuita entry point
// used when leaving the Asia Travel Company (act02_arar.scn transitions to D000 entry 2).
constexpr std::uint32_t kDobuitaAsiaTravelEntryPoint = 2;
constexpr std::uint8_t kDaytimeHour = 10;

constexpr std::array kTattooFlags{
    StoryFlag{SHENMUE_FLAG_TATQ_FOLLOWUP_QUEUED, true},
    StoryFlag{SHENMUE_FLAG_TATQ_FOLLOWUP_COMPLETE, false},
};
constexpr std::array kDobuitaNightFlags{
    StoryFlag{SHENMUE_FLAG_D000_HATO_STOPPER_CLEARED, false},
    StoryFlag{SHENMUE_FLAG_JU00_CATA1_NBO_BRANCH_CHOSEN, true},
};
constexpr std::array kDobuitaJimmyFlags{
    StoryFlag{SHENMUE_FLAG_GMCT_ROUTE_STARTED, true},
    StoryFlag{SHENMUE_FLAG_D000_JIMSE_EVENT_DONE, false},
};
constexpr std::array kWarehouseDistrictFlags{
    StoryFlag{SHENMUE_FLAG_MS08_SCENE_EXIT_COMPLETE, false},
};
constexpr std::array kHarborGoroFlags{
    StoryFlag{kFlagHarborGoroEventArmed, true},
    StoryFlag{SHENMUE_FLAG_MFSY_GORO_QTE_COMPLETE, false},
};
constexpr std::array kHarborHisakaFlags{
    StoryFlag{SHENMUE_FLAG_MFSY_HISA_REQUEST_ACCEPTED, true},
    StoryFlag{SHENMUE_FLAG_MFSY_HISA_QTE_RESOLVED, false},
    StoryFlag{SHENMUE_FLAG_MFSY_HISA_REQUEST_DECLINED, false},
    StoryFlag{SHENMUE_FLAG_MS08_SCENE_EXIT_COMPLETE, false},
    StoryFlag{SHENMUE_FLAG_NOTE_WHAT_HAPPENED_TO_MAI, false},
};
constexpr std::array kHarborJobFlags{
    StoryFlag{SHENMUE_FLAG_MFSY_FORKLIFT_JOB_ACTIVE, true},
    StoryFlag{SHENMUE_FLAG_NOTE_FIRED_FROM_JOB, false},
    StoryFlag{SHENMUE_FLAG_PARTTIME_JOB_WAKE_ROUTE_ACTIVE, true},
    StoryFlag{kFlagHarborJobQteBlockedA, false},
    StoryFlag{kFlagHarborJobQteBlockedB, false},
};
constexpr std::array kHarborBrotherFlags{
    StoryFlag{SHENMUE_FLAG_MFSY_FORKLIFT_JOB_ACTIVE, true},
    StoryFlag{SHENMUE_FLAG_NOTE_FIRED_FROM_JOB, false},
    StoryFlag{SHENMUE_FLAG_NOTE_MARKS_BROTHER_KILLED, true},
    StoryFlag{SHENMUE_FLAG_NOTE_LAN_DI_STILL_IN_HARBOR, false},
    StoryFlag{kFlagHarborBrotherQteBlocked, false},
};
constexpr std::array kLongZhaFlags{
    StoryFlag{SHENMUE_FLAG_NOTE_TWO_FOREIGNERS_IN_LONG_ZHA, true},
    StoryFlag{SHENMUE_FLAG_NOTE_FIND_TERRY, false},
};

const std::array kPresets{
    StoryPreset{"Акт 1 · Tattoo Parlor [TATQ]", 1, {'T', 'A', 'T', 'Q'}, kDefaultEntryPoint, kActOneStep, std::nullopt, kTattooFlags},
    StoryPreset{"Акт 1 · Dobuita [D000] · ночная погоня", 1, {'D', '0', '0', '0'}, kDefaultEntryPoint, kActOneStep, kNightHour, kDobuitaNightFlags},
    StoryPreset{"Акт 2 · Old Warehouse District [MKSG]", 2, {'M', 'K', 'S', 'G'}, kDefaultEntryPoint, kActTwoStep, std::nullopt, kWarehouseDistrictFlags},
    StoryPreset{"Акт 2 · Dobuita [D000] · погоня за Джимми", 2, {'D', '0', '0', '0'}, kDobuitaAsiaTravelEntryPoint, kActTwoStep, kDaytimeHour,
        kDobuitaJimmyFlags},
    StoryPreset{"Акт 2 · New Yokosuka Harbor [MFSY] · Горо", 2, {'M', 'F', 'S', 'Y'}, kDefaultEntryPoint, kActTwoStep, std::nullopt, kHarborGoroFlags},
    StoryPreset{"Акт 2 · New Yokosuka Harbor [MFSY] · Хисака", 2, {'M', 'F', 'S', 'Y'}, kDefaultEntryPoint, kActTwoStep, std::nullopt, kHarborHisakaFlags,
        kHarborHisakaTrigger},
    StoryPreset{"Акт 3 · New Yokosuka Harbor [MFSY] · работа", 3, {'M', 'F', 'S', 'Y'}, kDefaultEntryPoint, kActThreeStep, kNightHour, kHarborJobFlags,
        kHarborJobTrigger},
    StoryPreset{"Акт 3 · New Yokosuka Harbor [MFSY] · брат Марка", 3, {'M', 'F', 'S', 'Y'}, kDefaultEntryPoint, kActThreeStep, kNightHour,
        kHarborBrotherFlags, kHarborBrotherTrigger},
    StoryPreset{"Акт 3 · MA00 · Long Zha", 3, {'M', 'A', '0', '0'}, kDefaultEntryPoint, kActThreeStep, std::nullopt, kLongZhaFlags},
};

// ---------------------------------------------------------------------------------------------
// Launch
// ---------------------------------------------------------------------------------------------

// Frames the area gets after its loading screen before Ryo is moved.
constexpr std::int32_t kTeleportSettleFrames = 30;

std::size_t g_selected = 0;
bool g_resetRequested = false;
std::optional<std::size_t> g_pendingPreset;
// Preset whose teleport is still to happen, and whether its area has loaded.
std::optional<std::size_t> g_pendingTeleport;
bool g_teleportAreaLoaded = false;
std::int32_t g_teleportSettledFrames = 0;
std::string g_status;

bool InPresetArea(const StoryPreset &preset) {
    return g_sceneId == preset.sceneId && g_areaId == make_shen_name(preset.area[0], preset.area[1], preset.area[2], preset.area[3]);
}

void Teleport(const StoryPreset &preset) {
    auto *const control = g_mapControlTask ? reinterpret_cast<MapControlData *>(g_mapControlTask->task_data_ptr) : nullptr;
    auto *const ryo = control ? static_cast<Character *>(control->ryo) : nullptr;
    if (!ryo) {
        g_status = "Не удалось найти Рё для телепорта";
        return;
    }
    Vector3 position = ryo->entity.node.position;
    position.x = preset.teleport->x;
    position.z = preset.teleport->z;
    // Same ground query the character movement uses.
    position.y = mapUpdateIntersectionStateAtLocalPoint(position);
    shenApplyTranslationToAll(ryo->entity.node, position);
    g_status = std::string("Сцена: ") + preset.label;
}

void SetFlag(const StoryFlag &flag) {
    // Same as Script_scenUtils command 12: a set flag also updates its notebook entry and item group.
    if (flag.value)
        MMTK_UpdateEntryForFlag(flag.id);
    const auto mask = static_cast<std::uint8_t>(1U << (flag.id & 7U));
    std::uint8_t &byte = g_shenmueFlags[flag.id >> 3U];
    byte = flag.value ? static_cast<std::uint8_t>(byte | mask) : static_cast<std::uint8_t>(byte & ~mask);
    RegisterInventoryItemGroup(flag.id);
}

// ---------------------------------------------------------------------------------------------
// Weather: the scripts' interpolated override (WEXE_SetInterpolatedWeatherOverride), per layer.
// ---------------------------------------------------------------------------------------------

enum WeatherLayer : std::int32_t { kWeatherCloud = 0, kWeatherRain = 1, kWeatherSnow = 2 };

struct WeatherPreset {
    const char *label;
    // Target intensities (0..1); nullopt hands the layer back to the calendar weather.
    std::optional<float> cloud;
    std::optional<float> rain;
    std::optional<float> snow;
};

constexpr float kWeatherOff = 0.0f;
constexpr float kWeatherFull = 1.0f;
// Frames the weather takes to blend to the chosen state.
constexpr std::int32_t kWeatherTransitionFrames = 120;
// WEXE_SetInterpolatedWeatherOverride's second argument: blend back to the calendar data.
constexpr std::int32_t kWeatherFollowCalendar = 1;
constexpr std::int32_t kWeatherUseTarget = 0;

const std::array kWeatherPresets{
    WeatherPreset{"По календарю", std::nullopt, std::nullopt, std::nullopt},
    WeatherPreset{"Ясно", kWeatherOff, kWeatherOff, kWeatherOff},
    WeatherPreset{"Облачно", kWeatherFull, kWeatherOff, kWeatherOff},
    WeatherPreset{"Дождь", kWeatherFull, kWeatherFull, kWeatherOff},
    WeatherPreset{"Снег", kWeatherFull, kWeatherOff, kWeatherFull},
};
constexpr std::size_t kCalendarWeather = 0;

std::size_t g_selectedWeather = kCalendarWeather;
// The chosen weather still has to be applied (just picked, or a new area recreated the weather).
bool g_weatherDirty = false;

bool WeatherRunning() {
    return g_cloudGameData && g_activeRainEntry && g_activeSnowEntry;
}

void ApplyWeatherLayer(WeatherLayer layer, const std::optional<float> &intensity) {
    WEXE_SetInterpolatedWeatherOverride(layer, intensity ? kWeatherUseTarget : kWeatherFollowCalendar, kWeatherTransitionFrames, intensity.value_or(kWeatherOff));
}

void ApplyWeather() {
    const WeatherPreset &preset = kWeatherPresets[g_selectedWeather];
    ApplyWeatherLayer(kWeatherCloud, preset.cloud);
    ApplyWeatherLayer(kWeatherRain, preset.rain);
    ApplyWeatherLayer(kWeatherSnow, preset.snow);
}

// ---------------------------------------------------------------------------------------------
// Dear ImGui
// ---------------------------------------------------------------------------------------------

constexpr float kFontPixels = 18.0f;
constexpr float kPanelMargin = 12.0f;
constexpr float kComboWidth = 460.0f;
constexpr const char *kFontFile = "Fonts\\segoeui.ttf";

bool g_imguiReady = false;

void InitializeImGui() {
    if (g_imguiReady || !g_windowHandle || !::shenmue::d3t::g_D3DDevice || !::shenmue::d3t::g_immediateContext)
        return;
    IMGUI_CHECKVERSION();
    ImGui::CreateContext();
    ImGuiIO &io = ImGui::GetIO();
    io.IniFilename = nullptr;
    io.LogFilename = nullptr;

    std::array<char, MAX_PATH> windows{};
    const UINT length = GetWindowsDirectoryA(windows.data(), static_cast<UINT>(windows.size()));
    const std::filesystem::path font = length ? std::filesystem::path(windows.data()) / kFontFile : std::filesystem::path{};
    std::error_code error;
    if (!font.empty() && std::filesystem::is_regular_file(font, error))
        io.Fonts->AddFontFromFileTTF(font.string().c_str(), kFontPixels, nullptr, io.Fonts->GetGlyphRangesCyrillic());
    else
        io.Fonts->AddFontDefault();

    ImGui_ImplWin32_Init(g_windowHandle);
    ImGui_ImplDX11_Init(static_cast<ID3D11Device *>(::shenmue::d3t::g_D3DDevice), ::shenmue::d3t::g_immediateContext);
    g_imguiReady = true;
}

void DrawPanel() {
    ImGui::SetNextWindowPos(ImVec2(kPanelMargin, kPanelMargin), ImGuiCond_FirstUseEver);
    ImGui::Begin("QTE", nullptr,
        ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_AlwaysAutoResize | ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoMove);
    ImGui::SetNextItemWidth(kComboWidth);
    if (ImGui::BeginCombo("##qte", kPresets[g_selected].label)) {
        for (std::size_t index = 0; index < kPresets.size(); ++index) {
            const bool selected = index == g_selected;
            if (ImGui::Selectable(kPresets[index].label, selected))
                g_selected = index;
            if (selected)
                ImGui::SetItemDefaultFocus();
        }
        ImGui::EndCombo();
    }
    ImGui::BeginDisabled(g_resetRequested || g_pendingPreset.has_value());
    if (ImGui::Button("Запустить")) {
        g_resetRequested = true;
        g_status = "Перезапуск...";
    }
    ImGui::EndDisabled();
    if (!g_status.empty()) {
        ImGui::SameLine();
        ImGui::TextUnformatted(g_status.c_str());
    }

    ImGui::SetNextItemWidth(kComboWidth);
    if (ImGui::BeginCombo("Погода", kWeatherPresets[g_selectedWeather].label)) {
        for (std::size_t index = 0; index < kWeatherPresets.size(); ++index) {
            const bool selected = index == g_selectedWeather;
            if (ImGui::Selectable(kWeatherPresets[index].label, selected) && !selected) {
                g_selectedWeather = index;
                g_weatherDirty = true;
            }
            if (selected)
                ImGui::SetItemDefaultFocus();
        }
        ImGui::EndCombo();
    }
    ImGui::End();
}

bool IsMouseMessage(UINT message) {
    return (message >= WM_MOUSEFIRST && message <= WM_MOUSELAST) || message == WM_MOUSEHWHEEL;
}

} // namespace

void QteSelect_Update() {
    // Skip the boot splash screens (SEGA / d3t logos, legal text) and their replay after a soft reset.
    g_skipFrontendSplash = true;
    if (g_weatherDirty && WeatherRunning()) {
        g_weatherDirty = false;
        ApplyWeather();
    }
    if (g_pendingTeleport && g_teleportAreaLoaded) {
        const StoryPreset &preset = kPresets[*g_pendingTeleport];
        if (!InPresetArea(preset)) {
            g_pendingTeleport.reset();
        } else if (CDTT_IsLoadingUIActive()) {
            g_teleportSettledFrames = 0;
        } else if (++g_teleportSettledFrames >= kTeleportSettleFrames) {
            g_pendingTeleport.reset();
            Teleport(preset);
        }
    }
    if (!g_resetRequested)
        return;
    g_resetRequested = false;
    const StoryPreset &preset = kPresets[g_selected];
    g_mapLaunchOptions = {true, preset.sceneId, shen_name{make_shen_name(preset.area[0], preset.area[1], preset.area[2], preset.area[3])},
        preset.entryPoint};
    g_pendingPreset = g_selected;
    g_status = "Загрузка локации...";
    // Tear down the UI (title layers, pause, subtitles) the way a return to title does, then boot
    // through Audio_Module instead of the title so the map launch options take effect.
    ReturnToTitleScreen();
    HLib_UpdateTaskQueue(SystemModule::Audio);
}

void QteSelect_ApplyPendingPreset() {
    if (!g_pendingPreset)
        return;
    const StoryPreset &preset = kPresets[*g_pendingPreset];
    g_pendingPreset.reset();
    for (const StoryFlag &flag : preset.flags)
        SetFlag(flag);
    if (preset.scenarioStep)
        SCEN_SetCurrentScenarioStep(*preset.scenarioStep);
    if (preset.hour) {
        // Like the scripts do: set the clock and the time it may run to (the end of the day).
        SetDateAndTime(*preset.hour, 0, kDayEndHour, kDayEndMinute);
        // The new-game defaults put the loading screen in GameplayDateSetup, which resets the clock
        // to 8:30 when it shows; the regular area-transition mode keeps the time.
        CDTT_SetMode(CDTTMode::None);
    }
    // One-shot: a later soft reset must boot normally again.
    g_mapLaunchOptions.active = false;
    g_status = std::string("Сцена: ") + preset.label;
    if (preset.teleport) {
        g_pendingTeleport = static_cast<std::size_t>(&preset - kPresets.data());
        g_teleportAreaLoaded = false;
        g_teleportSettledFrames = 0;
        g_status = "Загрузка локации...";
    }
}

void QteSelect_OnMapScriptLoaded() {
    // Each area recreates the weather system without overrides; re-apply a chosen weather.
    if (g_selectedWeather != kCalendarWeather)
        g_weatherDirty = true;
    if (g_pendingTeleport && InPresetArea(kPresets[*g_pendingTeleport])) {
        g_teleportAreaLoaded = true;
        g_teleportSettledFrames = 0;
    }
}

void QteSelect_Render() {
    InitializeImGui();
    if (!g_imguiReady)
        return;

    // The game hides the OS cursor while a pad is in use; draw ImGui's own one then.
    CURSORINFO cursor{.cbSize = sizeof(CURSORINFO)};
    ImGui::GetIO().MouseDrawCursor = GetCursorInfo(&cursor) && (cursor.flags & CURSOR_SHOWING) == 0;

    ImGui_ImplDX11_NewFrame();
    ImGui_ImplWin32_NewFrame();
    ImGui::NewFrame();
    DrawPanel();
    ImGui::Render();

    auto *const swapChain = static_cast<IDXGISwapChain *>(::shenmue::d3t::GetNativeSwapChain());
    auto *const device = static_cast<ID3D11Device *>(::shenmue::d3t::g_D3DDevice);
    ID3D11DeviceContext *const context = ::shenmue::d3t::g_immediateContext;
    ComPtr<ID3D11Texture2D> backBuffer;
    ComPtr<ID3D11RenderTargetView> target;
    if (!swapChain || FAILED(swapChain->GetBuffer(0, IID_PPV_ARGS(&backBuffer))) ||
        FAILED(device->CreateRenderTargetView(backBuffer.Get(), nullptr, &target))) {
        return;
    }
    ComPtr<ID3D11RenderTargetView> previousTarget;
    ComPtr<ID3D11DepthStencilView> previousDepth;
    context->OMGetRenderTargets(1, &previousTarget, &previousDepth);
    ID3D11RenderTargetView *const targets[] = {target.Get()};
    context->OMSetRenderTargets(1, targets, nullptr);
    ImGui_ImplDX11_RenderDrawData(ImGui::GetDrawData());
    ID3D11RenderTargetView *const restored[] = {previousTarget.Get()};
    context->OMSetRenderTargets(1, restored, previousDepth.Get());
}

bool QteSelect_HandleWindowMessage(void *window, std::uint32_t message, std::uint64_t wParam, std::int64_t lParam) {
    if (!g_imguiReady)
        return false;
    if (ImGui_ImplWin32_WndProcHandler(static_cast<HWND>(window), message, static_cast<WPARAM>(wParam), static_cast<LPARAM>(lParam)))
        return true;
    // Clicks and moves over the panel are not game input.
    return IsMouseMessage(message) && ImGui::GetIO().WantCaptureMouse;
}

} // namespace shenmue::s1::enhancements

#endif
