#define AppName "GT7 Race Control Écurie"
#define AppVersion "0.1.0"
#define AppExe "GT7-Race-Control-Ecurie.exe"
[Setup]
AppId={{D31D124A-11B1-4C80-9E0A-9B9FBB2BB0A3}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\GT7 Race Control Ecurie
DefaultGroupName={#AppName}
OutputDir=Output
OutputBaseFilename=GT7-Race-Control-Ecurie-Setup-{#AppVersion}
Compression=lzma
SolidCompression=yes
PrivilegesRequired=lowest
[Files]
Source: "dist\GT7-Race-Control-Ecurie\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon
[Tasks]
Name: "desktopicon"; Description: "Créer un raccourci sur le Bureau"; Flags: unchecked
[Run]
Filename: "{app}\{#AppExe}"; Description: "Lancer {#AppName}"; Flags: nowait postinstall skipifsilent
