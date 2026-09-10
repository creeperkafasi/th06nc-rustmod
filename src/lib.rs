use std::{
    fs::{File, OpenOptions},
    io::Write,
    mem,
    ptr::null,
    sync::{
        LazyLock, Mutex,
        atomic::{AtomicIsize, Ordering},
    },
};

use log::Level;
use windows_sys::Win32::{
    Foundation::{HWND, LPARAM, LRESULT, WPARAM},
    System::{
        LibraryLoader::GetModuleHandleA,
        Memory::{PAGE_READWRITE, VirtualProtect},
        Threading::Sleep,
    },
    UI::{
        Input::KeyboardAndMouse::{VIRTUAL_KEY, VK_1},
        WindowsAndMessaging::{
            CallWindowProcA, GWLP_WNDPROC, GetWindowTextA, MB_OK, MessageBoxA, SetWindowLongPtrA,
            SetWindowTextA, WM_KEYDOWN,
        },
    },
};

struct FileLogger {
    file: Mutex<File>,
}
impl log::Log for FileLogger {
    fn enabled(&self, metadata: &log::Metadata) -> bool {
        metadata.level() <= Level::Info
    }

    fn log(&self, record: &log::Record) {
        let mut f = self.file.lock().unwrap();
        f.write_fmt(format_args!(
            "{}|{}@[{}]: {}\n",
            chrono::Utc::now().format("%d.%m.%Y %H:%M:%S"),
            record.level(),
            record.module_path().unwrap(),
            record.args()
        ))
        .unwrap();
    }

    fn flush(&self) {
        self.file.lock().unwrap().flush().unwrap();
    }
}

static LOGGER: LazyLock<FileLogger> = LazyLock::new(|| FileLogger {
    file: Mutex::new(
        OpenOptions::new()
            .append(true)
            .create(true)
            .open("mod.log")
            .expect("Unable to open log file"),
    ),
});

mod modloader {
    use std::{arch::global_asm, ffi::CString, os::raw::c_void, ptr::null_mut};

    use windows_sys::Win32::{
        Foundation::{HINSTANCE, HWND, LPARAM},
        System::{
            LibraryLoader::{GetProcAddress, LoadLibraryA},
            SystemServices::{DLL_PROCESS_ATTACH, DLL_PROCESS_DETACH},
            Threading::{GetCurrentProcessId, Sleep},
        },
        UI::WindowsAndMessaging::{EnumWindows, GetWindowThreadProcessId},
    };

    unsafe extern "C" {
        unsafe static mut REAL_CXX_FRAME_HANDLER: usize;
        unsafe static mut REAL_NLG_DISPATCH: usize;
        unsafe static mut REAL_NLG_RETURN: usize;
    }

    global_asm!(
        ".data",
        ".globl REAL_CXX_FRAME_HANDLER",
        "REAL_CXX_FRAME_HANDLER: .quad 0",
        ".globl REAL_NLG_DISPATCH",
        "REAL_NLG_DISPATCH: .quad 0",
        ".globl REAL_NLG_RETURN",
        "REAL_NLG_RETURN: .quad 0",
        ".text",
        ".globl __CxxFrameHandler4",
        "__CxxFrameHandler4:",
        "    mov rax, [rip + REAL_CXX_FRAME_HANDLER]",
        "    jmp rax",
        ".globl __NLG_Dispatch2",
        "__NLG_Dispatch2:",
        "    mov rax, [rip + REAL_NLG_DISPATCH]",
        "    jmp rax",
        ".globl __NLG_Return2",
        "__NLG_Return2:",
        "    mov rax, [rip + REAL_NLG_RETURN]",
        "    jmp rax",
    );

    fn fix_real_exports() {
        let path = CString::new("C:\\Windows\\System32\\vcruntime140_1.dll").unwrap();
        let real_dll = unsafe { LoadLibraryA(path.as_ptr() as *const u8) };

        if real_dll != std::ptr::null_mut() {
            unsafe {
                REAL_CXX_FRAME_HANDLER = GetProcAddress(
                    real_dll,
                    CString::new("__CxxFrameHandler4").unwrap().as_ptr() as _,
                )
                .unwrap() as usize;
                REAL_NLG_DISPATCH = GetProcAddress(
                    real_dll,
                    CString::new("__NLG_Dispatch2").unwrap().as_ptr() as _,
                )
                .unwrap() as usize;
                REAL_NLG_RETURN = GetProcAddress(
                    real_dll,
                    CString::new("__NLG_Return2").unwrap().as_ptr() as _,
                )
                .unwrap() as usize;
            }
        }
    }

    pub struct InitPhaseOnly(());

    #[unsafe(no_mangle)]
    #[allow(non_snake_case, unused_variables)]
    extern "system" fn DllMain(dll_module: HINSTANCE, call_reason: u32, _: *mut ()) -> bool {
        fix_real_exports();

        match call_reason {
            DLL_PROCESS_ATTACH => {
                unsafe {
                    super::apply_patches(InitPhaseOnly(()));
                    windows_sys::Win32::System::Threading::CreateThread(
                        null_mut(),
                        0,
                        Some(_mod_thread),
                        null_mut(),
                        0,
                        null_mut(),
                    )
                };
            }
            DLL_PROCESS_DETACH => (),
            _ => (),
        }

        true
    }

    unsafe extern "system" fn _mod_thread(_param: *mut c_void) -> u32 {
        log::info!("Waiting for window to be found");
        let window = loop {
            if let Some(hwnd) = find_game_window() {
                break hwnd;
            }

            unsafe { Sleep(100) };
        };
        log::info!("HWND found: {window:?}");

        let mod_main: fn(HWND) -> ! = super::main;
        mod_main(window);
    }

    fn find_game_window() -> Option<HWND> {
        #[repr(C)]
        struct WindowCtx {
            pid: u32,
            found: Option<HWND>,
        }

        extern "system" fn lpenumfunc(hwnd: HWND, lparam: isize) -> i32 {
            let ctx = unsafe { (lparam as *mut WindowCtx).as_mut_unchecked() };
            let mut pid: u32 = 0;
            unsafe { GetWindowThreadProcessId(hwnd, &raw mut pid) };
            if pid != ctx.pid {
                1
            } else {
                ctx.found = Some(hwnd);
                0
            }
        }

        let mut ctx: WindowCtx = WindowCtx {
            pid: unsafe { GetCurrentProcessId() },
            found: None,
        };
        unsafe { EnumWindows(Some(lpenumfunc), (&raw mut ctx) as LPARAM) };
        ctx.found
    }
}

fn apply_patches(lock: modloader::InitPhaseOnly) {
    unsafe {
        Patch::<f64> {
            addr: 0x30ce38,
            value: 30.0,
        }
        .apply(lock)
        .expect("Failed to patch FPS");
    };
}

fn main(window: HWND) -> ! {
    log::set_logger(&*LOGGER).unwrap();
    log::set_max_level(log::LevelFilter::Info);

    log::info!("Touhou EoSD New Classic mod loader by creeperkafasi");

    // Set title
    {
        let mut title_bytes = [0; 128];
        unsafe { GetWindowTextA(window, title_bytes.as_mut_ptr(), title_bytes.len() as i32) };
        let original_title = String::from_utf8(title_bytes.to_vec()).unwrap();
        unsafe { SetWindowTextA(window, format!("[MODDED] {original_title}").as_ptr()) };
    }

    // Inject Mod WndProc
    {
        let ptr =
            unsafe { SetWindowLongPtrA(window, GWLP_WNDPROC, wnd_proc as *const () as isize) };
        ORIGINAL_WNDPROC.store(ptr, Ordering::Relaxed);
    }

    loop {
        unsafe { Sleep(1000) };
    }
}

static ORIGINAL_WNDPROC: AtomicIsize = AtomicIsize::new(0);

extern "system" fn wnd_proc(hwnd: HWND, msg: u32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    match msg {
        WM_KEYDOWN => match wparam as VIRTUAL_KEY {
            VK_1 => {
                unsafe { MessageBoxA(hwnd, "Button 1 Pressed".as_ptr(), "Title".as_ptr(), MB_OK) };
            }
            _ => {}
        },
        _ => {}
    }

    unsafe {
        CallWindowProcA(
            mem::transmute(ORIGINAL_WNDPROC.load(Ordering::Relaxed)),
            hwnd,
            msg,
            wparam,
            lparam,
        )
    }
}

struct Patch<T>
where
    T: Sized,
{
    addr: usize,
    value: T,
}

#[derive(Debug)]
enum PatchError {
    GetModuleHandle,
    ProtectionDisable,
    ProtectionEnable,
}

impl<T: Sized> Patch<T> {
    unsafe fn apply(self, _lock: modloader::InitPhaseOnly) -> Result<T, PatchError> {
        let main = unsafe { GetModuleHandleA(null()) };
        if main.is_null() {
            return Err(PatchError::GetModuleHandle);
        }
        let addr: *mut T = unsafe { main.byte_add(self.addr) }.cast();

        let oldvalue = unsafe { addr.read() };

        // DISABLE WRITE PROTECTION
        let mut old_prot = 0;
        if unsafe {
            VirtualProtect(
                addr.cast(),
                size_of::<T>(),
                PAGE_READWRITE,
                &raw mut old_prot,
            )
        } == 0
        {
            return Err(PatchError::ProtectionDisable);
        }

        // WRITE
        unsafe { addr.write(self.value) };

        // REENABLE WRITE PROTECTION
        if unsafe { VirtualProtect(addr.cast(), size_of::<T>(), old_prot, &raw mut old_prot) } == 0
        {
            return Err(PatchError::ProtectionEnable);
        }

        Ok(oldvalue)
    }
}
