use std::fs::File;
use std::io::Read;

use proc_macro::TokenStream;
use quote::format_ident;
use unsynn::{LiteralString, ToTokens};

use unsynn::Parse;

#[proc_macro]
pub fn dll_proxy(ts: TokenStream) -> TokenStream {
    let mut iter = unsynn::TokenStream::from(ts).to_token_iter();

    let dll_lit = LiteralString::parse(&mut iter).expect("Expected string");
    let dll_name = dll_lit.as_str();

    let buf = {
        let mut dll_file = get_dll(&dll_name).expect("dll file not found");
        let mut buf: Vec<u8> = Vec::new();
        dll_file.read_to_end(&mut buf).unwrap();
        buf
    };

    let object = goblin::Object::parse(&buf).unwrap();

    let exports = match object {
        goblin::Object::PE(pe) => pe.exports,
        _ => todo!(),
    };

    let names: Vec<&str> = exports.iter().filter_map(|e| e.name).collect();

    let data: Vec<String> = names
        .iter()
        .map(|name| {
            [
                format!(".globl REAL_{name}"),
                format!("REAL_{name}: .quad 0",),
            ]
        })
        .flatten()
        .collect();

    let text: Vec<String> = names
        .iter()
        .map(|name| {
            [
                format!(".globl {name}"),
                format!("{name}:"),
                format!("   mov rax, [rip + REAL_{name}]"),
                format!("   jmp rax"),
            ]
        })
        .flatten()
        .collect();

    let globals: Vec<_> = names
        .iter()
        .map(|name| {
            let ident = format_ident!("REAL_{name}");
            quote::quote! {
                unsafe static mut #ident: usize
            }
        })
        .collect();

    let path = format!(r"C:\Windows\System32\{dll_name}");

    let set_global: Vec<_> = names
        .iter()
        .map(|name| {
            let ident = format_ident!("REAL_{name}");
            quote::quote! {
                #ident = GetProcAddress(
                    real_dll,
                    CString::new(#name).unwrap().as_ptr() as _,
                )
                .unwrap() as usize;
            }
        })
        .collect();
    // panic!("{:#?}\n{:#?}\n{:#?}", data, text, globals);

    let out = quote::quote! {
        unsafe extern "C" {
            #(#globals;)*
        }

        global_asm!(
            ".data",
            #(#data,)*
            ".text",
            #(#text,)*
        );

        fn fix_real_exports() {
            let path = CString::new(#path).unwrap();
            let real_dll = unsafe { LoadLibraryA(path.as_ptr() as *const u8) };

            unsafe {
                #(#set_global;)*
            }
        }

    };
    out.into()
}

fn get_dll(name: &str) -> Result<File, std::io::Error> {
    if cfg!(target_os = "windows") {
        File::open(format!(r"C:\Windows\System32\{name}"))
    } else if cfg!(target_os = "linux") {
        let default_wineprefix = std::env::var("HOME")
            .map(|home| format!("{home}/.wine"))
            .unwrap_or_else(|_| String::from(".wine"));
        let wineprefix = std::env::var("WINEPREFIX").unwrap_or(default_wineprefix);
        let dll_path = format!("{wineprefix}/drive_c/windows/system32/{name}");
        File::open(dll_path)
    } else {
        todo!()
    }
}
