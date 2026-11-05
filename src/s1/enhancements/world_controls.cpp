#include "s1/enhancements/world_controls.hpp"

#if SHENMUE_ENHANCEMENT_S1_QTE_SELECT && SHENMUE_IS_S1 && SHENMUE_IS_PC && defined(_WIN32)

#include "s1/date_time.hpp"
#include "s1/task/time.hpp"
#include "s1/task/wexe.hpp"

#include "imgui.h"

#include <array>
#include <cstdint>
#include <cstdio>
#include <optional>

namespace shenmue::s1::enhancements {
namespace {

// ---------------------------------------------------------------------------------------------
// Weather: the scripts' interpolated override (WEXE_SetInterpolatedWeatherOverride), per layer.
// The rain override also drives the wind, as the weather tick does.
// ---------------------------------------------------------------------------------------------

enum WeatherLayer : std::int32_t { kWeatherCloud = 0, kWeatherRain = 1, kWeatherSnow = 2 };

struct WeatherState {
    // Target intensities (0..1); nullopt hands the layer back to the calendar weather.
    std::optional<float> cloud;
    std::optional<float> rain;
    std::optional<float> snow;
};

struct WeatherPreset {
    const char *label;
    WeatherState state;
};

constexpr float kOff = 0.0f;
constexpr float kLight = 0.3f;
constexpr float kMedium = 0.6f;
constexpr float kOvercast = 0.7f;
constexpr float kFull = 1.0f;

const std::array kWeatherPresets{
    WeatherPreset{"По календарю", {std::nullopt, std::nullopt, std::nullopt}},
    WeatherPreset{"Ясно", {kOff, kOff, kOff}},
    WeatherPreset{"Переменная облачность", {kMedium, kOff, kOff}},
    WeatherPreset{"Пасмурно", {kFull, kOff, kOff}},
    WeatherPreset{"Небольшой дождь", {kOvercast, kLight, kOff}},
    WeatherPreset{"Дождь", {kFull, kMedium, kOff}},
    WeatherPreset{"Ливень", {kFull, kFull, kOff}},
    WeatherPreset{"Небольшой снег", {kOvercast, kOff, kLight}},
    WeatherPreset{"Снег", {kFull, kOff, kMedium}},
    WeatherPreset{"Метель", {kFull, kOff, kFull}},
};
constexpr std::size_t kCalendarWeather = 0;

// WEXE_SetInterpolatedWeatherOverride's second argument: blend back to the calendar data, or to a target.
constexpr std::int32_t kFollowCalendar = 1;
constexpr std::int32_t kUseTarget = 0;
constexpr std::int32_t kFramesPerSecond = 60;
constexpr float kDefaultTransitionSeconds = 2.0f;
constexpr float kMaximumTransitionSeconds = 30.0f;

std::size_t g_selectedWeather = kCalendarWeather;
bool g_customWeather = false;
std::array<float, 3> g_customIntensity{kFull, kOff, kOff};
float g_transitionSeconds = kDefaultTransitionSeconds;
// Weather active in game (what re-applies after an area load); nullopt = calendar.
std::optional<WeatherState> g_appliedWeather;
bool g_weatherDirty = false;

bool WeatherRunning() {
    return g_cloudGameData && g_activeRainEntry && g_activeSnowEntry;
}

void ApplyLayer(WeatherLayer layer, const std::optional<float> &intensity, std::int32_t frames) {
    WEXE_SetInterpolatedWeatherOverride(layer, intensity ? kUseTarget : kFollowCalendar, frames, intensity.value_or(kOff));
}

void ApplyWeather(const WeatherState &state) {
    const auto frames = static_cast<std::int32_t>(g_transitionSeconds * static_cast<float>(kFramesPerSecond));
    ApplyLayer(kWeatherCloud, state.cloud, frames);
    ApplyLayer(kWeatherRain, state.rain, frames);
    ApplyLayer(kWeatherSnow, state.snow, frames);
}

void ChooseWeather(const WeatherState &state) {
    g_appliedWeather = state;
    g_weatherDirty = true;
}

// ---------------------------------------------------------------------------------------------
// Time of day
// ---------------------------------------------------------------------------------------------

constexpr int kLastHour = 23;
constexpr int kLastMinute = 59;
// Latest time the clock may run to, as the scripts set it (Ryo is sent home at 23:30).
constexpr std::uint8_t kDayEndHour = 23;
constexpr std::uint8_t kDayEndMinute = 30;
constexpr int kDefaultHour = 12;

int g_hour = kDefaultHour;
int g_minute = 0;
bool g_timeRequested = false;
bool g_freeze = false;

constexpr float kWindowX = 12.0f;
constexpr float kWindowY = 290.0f;
constexpr float kSliderWidth = 300.0f;

} // namespace

void WorldControls_Update() {
    // Both need a running map: a new game resets the clock, and the weather system lives in the map.
    if (g_timeRequested && WeatherRunning()) {
        g_timeRequested = false;
        SetDateAndTime(static_cast<std::uint8_t>(g_hour), static_cast<std::uint8_t>(g_minute), kDayEndHour, kDayEndMinute);
        // The clock stops once it reaches the limit; restart it for the new time.
        TIME_EnableAdvance();
        // The calendar weather depends on the time; let it follow unless the weather is overridden.
        if (!g_appliedWeather)
            g_weatherDirty = true;
    }
    if (g_weatherDirty && WeatherRunning()) {
        g_weatherDirty = false;
        ApplyWeather(g_appliedWeather.value_or(WeatherState{}));
    }
}

void WorldControls_OnMapScriptLoaded() {
    if (g_appliedWeather)
        g_weatherDirty = true;
}

void DrawWorldControls() {
    ImGui::SetNextWindowPos(ImVec2(kWindowX, kWindowY), ImGuiCond_FirstUseEver);
    // Start collapsed so the game stays visible; the title bar opens it.
    ImGui::SetNextWindowCollapsed(true, ImGuiCond_FirstUseEver);
    if (!ImGui::Begin("Время и погода", nullptr, ImGuiWindowFlags_AlwaysAutoResize | ImGuiWindowFlags_NoSavedSettings)) {
        ImGui::End();
        return;
    }

    std::array<char, 32> now{};
    std::snprintf(now.data(), now.size(), "Сейчас: %02d:%02d", static_cast<int>(g_currentDateTime.hours), static_cast<int>(g_currentDateTime.minutes));
    ImGui::TextUnformatted(now.data());
    ImGui::SetNextItemWidth(kSliderWidth);
    ImGui::SliderInt("Час", &g_hour, 0, kLastHour);
    ImGui::SetNextItemWidth(kSliderWidth);
    ImGui::SliderInt("Минута", &g_minute, 0, kLastMinute);
    if (ImGui::Button("Установить время"))
        g_timeRequested = true;
    ImGui::SameLine();
    if (ImGui::Checkbox("Остановить время", &g_freeze))
        g_freezeTime = g_freeze ? 1 : 0;

    ImGui::Separator();
    ImGui::SetNextItemWidth(kSliderWidth);
    if (ImGui::BeginCombo("Погода", g_customWeather ? "Свои значения" : kWeatherPresets[g_selectedWeather].label)) {
        for (std::size_t index = 0; index < kWeatherPresets.size(); ++index) {
            const bool selected = !g_customWeather && index == g_selectedWeather;
            if (ImGui::Selectable(kWeatherPresets[index].label, selected)) {
                g_selectedWeather = index;
                g_customWeather = false;
                if (index == kCalendarWeather) {
                    g_appliedWeather.reset();
                    g_weatherDirty = true;
                } else {
                    ChooseWeather(kWeatherPresets[index].state);
                }
            }
        }
        ImGui::EndCombo();
    }
    ImGui::SetNextItemWidth(kSliderWidth);
    ImGui::SliderFloat("Переход, с", &g_transitionSeconds, 0.0f, kMaximumTransitionSeconds, "%.1f");

    if (ImGui::CollapsingHeader("Свои значения")) {
        ImGui::SetNextItemWidth(kSliderWidth);
        ImGui::SliderFloat("Облака", &g_customIntensity[kWeatherCloud], kOff, kFull, "%.2f");
        ImGui::SetNextItemWidth(kSliderWidth);
        ImGui::SliderFloat("Дождь и ветер", &g_customIntensity[kWeatherRain], kOff, kFull, "%.2f");
        ImGui::SetNextItemWidth(kSliderWidth);
        ImGui::SliderFloat("Снег", &g_customIntensity[kWeatherSnow], kOff, kFull, "%.2f");
        if (ImGui::Button("Применить")) {
            g_customWeather = true;
            ChooseWeather({g_customIntensity[kWeatherCloud], g_customIntensity[kWeatherRain], g_customIntensity[kWeatherSnow]});
        }
    }
    if (!WeatherRunning())
        ImGui::TextDisabled("Погода и время применятся в игре.");
    ImGui::End();
}

} // namespace shenmue::s1::enhancements

#endif
