#include "s1/enhancements/location_picker.hpp"

#if SHENMUE_ENHANCEMENT_S1_QTE_SELECT && SHENMUE_IS_S1 && SHENMUE_IS_PC && defined(_WIN32)

#include "common/archive_data.hpp"
#include "common/types.hpp"
#include "s1/task/ctrl.hpp"
#include "s1/ui_internal.hpp"

#include "imgui.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <string_view>
#include <vector>

namespace shenmue::s1::enhancements {
namespace {

struct SpawnArea {
    std::int32_t sceneId;
    std::uint32_t areaId;
    std::string label;
    std::vector<std::uint32_t> entryPoints;
};

// "/scene/NN/AAAA/mapinfo.bin" (cleaned archive names are lower case).
constexpr std::string_view kScenePrefix = "/scene/";
constexpr std::string_view kMapInfoSuffix = "/mapinfo.bin";
constexpr std::size_t kSceneDigits = 2;
constexpr std::size_t kAreaLength = 4;
constexpr std::size_t kMapInfoPathLength = kScenePrefix.size() + kSceneDigits + 1 + kAreaLength + kMapInfoSuffix.size();
// MAPE groups: entry points, -1, map ids, -1 (see the MAPE reader in ctrl.cpp).
constexpr std::int32_t kListEnd = -1;
constexpr std::uint32_t kDefaultEntryPoint = 0;
// Empty placeholder area shipped next to the real ones.
constexpr std::string_view kPlaceholderArea = "0000";

constexpr float kWindowX = 12.0f;
constexpr float kWindowY = 150.0f;
constexpr float kComboWidth = 360.0f;

std::string AreaDisplayName(std::string_view area) {
    for (const MapLocationToken &token : ui_runtime_detail::kMapLocationTokens) {
        if (token.areaId && area == token.areaId)
            return std::string(token.areaName) + " [" + std::string(area) + "]";
    }
    return std::string(area);
}

std::vector<std::uint32_t> ReadEntryPoints(const std::vector<std::byte> &mapInfo) {
    std::vector<std::uint32_t> entries;
    std::uint32_t size = 0;
    const std::byte *cursor = MAPINFO_FindChunkPayloadIn(mapInfo.data(), static_cast<std::uint32_t>(mapInfo.size()), SHEN_NAME(MAPE), &size);
    if (cursor) {
        const std::byte *const end = cursor + size;
        const auto read = [&](std::int32_t &value) {
            if (static_cast<std::size_t>(end - cursor) < sizeof(value))
                return false;
            std::memcpy(&value, cursor, sizeof(value));
            cursor += sizeof(value);
            return true;
        };
        std::int32_t value = kListEnd;
        while (cursor < end) {
            // Entry points of the group, then the maps they load.
            while (read(value) && value != kListEnd) {
                if (value >= 0 && std::find(entries.begin(), entries.end(), static_cast<std::uint32_t>(value)) == entries.end())
                    entries.push_back(static_cast<std::uint32_t>(value));
            }
            while (read(value) && value != kListEnd) {
            }
            if (value != kListEnd)
                break;
        }
    }
    if (entries.empty())
        entries.push_back(kDefaultEntryPoint);
    std::sort(entries.begin(), entries.end());
    return entries;
}

std::vector<SpawnArea> BuildAreas() {
    std::vector<SpawnArea> areas;
    for (const std::string &path : archive_data::ArchiveData_ListNamedPaths(kScenePrefix.data())) {
        if (path.size() != kMapInfoPathLength || !path.ends_with(kMapInfoSuffix))
            continue;
        const std::string scene = path.substr(kScenePrefix.size(), kSceneDigits);
        std::string area = path.substr(kScenePrefix.size() + kSceneDigits + 1, kAreaLength);
        if (!std::isdigit(static_cast<unsigned char>(scene[0])) || !std::isdigit(static_cast<unsigned char>(scene[1])))
            continue;
        std::transform(area.begin(), area.end(), area.begin(), [](unsigned char c) { return static_cast<char>(std::toupper(c)); });
        if (area == kPlaceholderArea)
            continue;
        const std::uint64_t size = archive_data::ArchiveData_GetFileSize(path.c_str());
        std::vector<std::byte> mapInfo(static_cast<std::size_t>(size));
        if (!size || !archive_data::ArchiveData_Read(path.c_str(), 0, mapInfo.data(), size))
            continue;
        const std::int32_t sceneId = std::atoi(scene.c_str());
        areas.push_back({sceneId, make_shen_name(area[0], area[1], area[2], area[3]),
            "Акт " + std::to_string(sceneId) + " · " + AreaDisplayName(area), ReadEntryPoints(mapInfo)});
    }
    return areas;
}

std::vector<SpawnArea> g_areas;
bool g_areasBuilt = false;
std::size_t g_selectedArea = 0;
std::size_t g_selectedEntry = 0;

} // namespace

void DrawLocationPicker() {
    if (!g_areasBuilt && archive_data::ArchiveData_HasArchives()) {
        g_areas = BuildAreas();
        g_areasBuilt = true;
    }
    ImGui::SetNextWindowPos(ImVec2(kWindowX, kWindowY), ImGuiCond_FirstUseEver);
    // Start collapsed so the game stays visible; the title bar opens it.
    ImGui::SetNextWindowCollapsed(true, ImGuiCond_FirstUseEver);
    if (!ImGui::Begin("Локации", nullptr, ImGuiWindowFlags_AlwaysAutoResize | ImGuiWindowFlags_NoSavedSettings)) {
        ImGui::End();
        return;
    }
    if (g_areas.empty()) {
        ImGui::TextUnformatted(g_areasBuilt ? "Локации не найдены" : "Ожидание архивов игры...");
        ImGui::End();
        return;
    }
    g_selectedArea = (std::min)(g_selectedArea, g_areas.size() - 1);
    const SpawnArea &area = g_areas[g_selectedArea];

    ImGui::SetNextItemWidth(kComboWidth);
    if (ImGui::BeginCombo("Локация", area.label.c_str())) {
        for (std::size_t index = 0; index < g_areas.size(); ++index) {
            const bool selected = index == g_selectedArea;
            if (ImGui::Selectable(g_areas[index].label.c_str(), selected)) {
                g_selectedArea = index;
                g_selectedEntry = 0;
            }
            if (selected)
                ImGui::SetItemDefaultFocus();
        }
        ImGui::EndCombo();
    }

    const SpawnArea &chosen = g_areas[g_selectedArea];
    g_selectedEntry = (std::min)(g_selectedEntry, chosen.entryPoints.size() - 1);
    const std::string entryLabel = "Точка " + std::to_string(chosen.entryPoints[g_selectedEntry]);
    ImGui::SetNextItemWidth(kComboWidth);
    if (ImGui::BeginCombo("Точка входа", entryLabel.c_str())) {
        for (std::size_t index = 0; index < chosen.entryPoints.size(); ++index) {
            const bool selected = index == g_selectedEntry;
            if (ImGui::Selectable(("Точка " + std::to_string(chosen.entryPoints[index])).c_str(), selected))
                g_selectedEntry = index;
            if (selected)
                ImGui::SetItemDefaultFocus();
        }
        ImGui::EndCombo();
    }
    if (ImGui::Button("Перейти"))
        RequestLocationJump(chosen.sceneId, chosen.areaId, chosen.entryPoints[g_selectedEntry]);
    ImGui::End();
}

} // namespace shenmue::s1::enhancements

#endif
