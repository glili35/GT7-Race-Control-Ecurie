#define AppName "GT7 Race Control Écurie (Test)"
#define AppVersion "0.9.0"
#define AppExe "GT7-Race-Control-Ecurie.exe"

[Setup]
; Canal de test séparé : ne remplace pas l'ancienne installation stable.
AppId={{8D4D0B18-56DC-4F66-B286-CE425D978ED2}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
VersionInfoVersion=0.9.0.0
DefaultDirName={localappdata}\Programs\GT7 Race Control Ecurie Test
DefaultGroupName={#AppName}
OutputDir=Output
OutputBaseFilename=GT7-Race-Control-Ecurie-Setup-{#AppVersion}-test
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#AppExe}
CloseApplications=yes

[Languages]
Name: "french"; MessagesFile: "compiler:Languages\French.isl"

[Files]
Source: "dist\GT7-Race-Control-Ecurie\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Créer un raccourci de la version de test sur le Bureau"; Flags: unchecked

[Run]
Filename: "{app}\{#AppExe}"; Description: "Lancer {#AppName}"; Flags: nowait postinstall skipifsilent
