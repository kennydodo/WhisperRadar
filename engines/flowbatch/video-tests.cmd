@echo off
rem Image-to-video comparison tests (run from engines\flowbatch). Results: output\video-test\
set P=--profile D:\Repos\flow-profile-b
set I=E:\YOUTUBE\POV FINANCE\TEST1PROJECT\flow_images

echo === 1/4 close-up, Omni 360p 4s
call npm.cmd run video -- %P% --image "%I%\S01_02_CU_ZI.png" --prompt "subtle natural movement, slight head turn, gentle breathing, soft camera push in" --out output\video-test\1_omni360_closeup.mp4

echo === 2/4 wide scene, Omni 720p 6s
call npm.cmd run video -- %P% --image "%I%\S03_01_SCN_ZO.png" --resolution 720p --duration 6s --prompt "slow camera pull back revealing the scene, ambient motion in the background" --out output\video-test\2_omni720_scene.mp4

echo === 3/4 same image as the first test, Veo 3.1 Lite
call npm.cmd run video -- %P% --image "%I%\S01_01_SCN_ZO.png" --model "Veo 3.1 - Lite" --prompt "slow gentle push in, subtle dust drifting in the air" --out output\video-test\3_veolite_scene.mp4

echo === 4/4 close-up, Omni 360p 4s, character action
call npm.cmd run video -- %P% --image "%I%\S04_01_CU_ZI.png" --prompt "the character looks up and gives a small confident smile, slight camera push in" --out output\video-test\4_omni360_action.mp4

echo Done. Files are in output\video-test\
pause
