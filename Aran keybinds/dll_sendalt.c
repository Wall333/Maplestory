// Minimal DLL that sends an Alt key press when loaded.
// Build with MSVC: cl /LD dll_sendalt.c user32.lib

#include <windows.h>

DWORD WINAPI SendAltThread(LPVOID lpParam) {
    // small delay to let process settle
    Sleep(100);

    INPUT input[2];
    ZeroMemory(input, sizeof(input));

    input[0].type = INPUT_KEYBOARD;
    input[0].ki.wVk = VK_MENU; // Alt down
    input[0].ki.dwFlags = 0;

    input[1].type = INPUT_KEYBOARD;
    input[1].ki.wVk = VK_MENU; // Alt up
    input[1].ki.dwFlags = KEYEVENTF_KEYUP;

    // send press and release with small delay
    SendInput(1, &input[0], sizeof(INPUT));
    Sleep(50);
    SendInput(1, &input[1], sizeof(INPUT));

    return 0;
}

BOOL WINAPI DllMain(HINSTANCE hinstDLL, DWORD fdwReason, LPVOID lpReserved) {
    switch (fdwReason) {
        case DLL_PROCESS_ATTACH: {
            // Create a thread to do work (avoid lengthy work in DllMain)
            HANDLE h = CreateThread(NULL, 0, SendAltThread, NULL, 0, NULL);
            if (h) CloseHandle(h);
            break;
        }
        case DLL_PROCESS_DETACH:
            break;
    }
    return TRUE;
}
