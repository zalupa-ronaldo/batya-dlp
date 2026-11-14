# Third-party software

The application code is MIT. Independently executed programs and bundled runtime components retain their own licenses. No nonfree FFmpeg build is used.

| Component | Upstream | License / source |
| --- | --- | --- |
| yt-dlp standalone | https://github.com/yt-dlp/yt-dlp | Unlicense; standalone release also contains third-party components. See upstream README licensing section and `yt-dlp-THIRD_PARTY_LICENSES.txt`. |
| FFmpeg/ffprobe | https://github.com/FFmpeg-Builds/FFmpeg-Builds | LGPL shared build; original DLLs, license files and documentation are retained. Build recipe/source links are recorded with the exact release in the tool manifest. FFmpeg source: https://github.com/FFmpeg/FFmpeg. |
| Deno | https://github.com/denoland/deno | MIT; upstream third-party notices included. |
| Python | https://www.python.org/ | PSF License; license included with packaged runtime. |
| FastAPI, Starlette, Pydantic, uvicorn and dependencies | https://pypi.org/ | Each package retains its own license, copied from installed distribution metadata into `licenses/python/`. |
| certifi | https://github.com/certifi/python-certifi | MPL-2.0; includes Mozilla CA bundle. |
| PyInstaller bootloader | https://pyinstaller.org/ | GPL-2.0-or-later with an exception permitting distribution of packaged applications under other licenses. |

`tools/manifest.json` records exact release URLs and verified hashes. The FFmpeg distribution is dynamically linked: the adjacent DLLs may be replaced with compatible user-built versions. Upstream license texts and documentation ship under `licenses/ffmpeg/`; source/build recipes are available at the exact upstream release URL in the manifest. The application invokes FFmpeg as an unmodified external process. See also `licenses/` for the complete notices collected during each build.
