; Inno Setup 6 x64 Installer Script for NotebookLM-Fleet

#ifndef MyAppVersion
  #define MyAppVersion "0.3.1"
#endif

#define MyAppName "NotebookLM-Fleet"
#define MyAppPublisher "NotebookLM-Fleet Open Source"
#define MyAppURL "https://github.com/wangsiyi7/NotebookLM-Fleet"
#define MyAppExeName "NotebookLM-Fleet.exe"

[Setup]
AppId={{8D6B4F1C-91F2-47A2-94D9-4E3A9B2F1D01}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} v{#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist\release
OutputBaseFilename=NotebookLM-Fleet-v{#MyAppVersion}-Setup-win64
SetupIconFile=assets\app.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/normal
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a Desktop shortcut (创建桌面快捷方式)"; GroupDescription: "Additional icons:"; Flags: checkedonce

[Files]
Source: "..\dist\NotebookLM-Fleet\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName} (启动 NotebookLM-Fleet)"; Flags: nowait postinstall skipifsilent
