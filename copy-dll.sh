STEAM="/old/steam/Steam/steamapps"
DLL="vcruntime140_1.dll"
TARGET="debug"

cp "target/x86_64-pc-windows-gnu/$TARGET/$DLL" "$STEAM/common/th06nc/$DLL"
echo "COPIED"