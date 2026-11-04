#pragma once

#if defined(_WIN32)

#include <mmdeviceapi.h>
#include <windows.h>

#include <cwchar>

namespace shenmue::platform {

// Position of Windows' default render device in an endpoint collection (0 when it is not found
// or lies beyond `count`). Endpoint enumeration order is unrelated to the default device, so the
// games list the default first: audio device setting 1 then follows the Windows choice.
inline UINT DefaultRenderEndpointIndex(IMMDeviceEnumerator *enumerator, IMMDeviceCollection *devices, UINT count) {
    if (!enumerator || !devices)
        return 0;
    IMMDevice *fallback = nullptr;
    if (FAILED(enumerator->GetDefaultAudioEndpoint(eRender, eConsole, &fallback)))
        return 0;
    LPWSTR defaultId = nullptr;
    const HRESULT idResult = fallback->GetId(&defaultId);
    fallback->Release();
    if (FAILED(idResult))
        return 0;
    UINT found = 0;
    for (UINT index = 0; index < count; ++index) {
        IMMDevice *device = nullptr;
        if (FAILED(devices->Item(index, &device)))
            continue;
        LPWSTR id = nullptr;
        const bool same = SUCCEEDED(device->GetId(&id)) && std::wcscmp(id, defaultId) == 0;
        CoTaskMemFree(id);
        device->Release();
        if (same) {
            found = index;
            break;
        }
    }
    CoTaskMemFree(defaultId);
    return found;
}

// Collection index to list in `slot`: the default device first, the others in enumeration order.
constexpr UINT EndpointForSlot(UINT slot, UINT defaultIndex) {
    if (slot == 0)
        return defaultIndex;
    return slot <= defaultIndex ? slot - 1 : slot;
}

} // namespace shenmue::platform

#endif
