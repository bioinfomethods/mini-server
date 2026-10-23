# mini-server

A tiny, read-only file server that lets browser-based genome viewers such as
[igv.js](https://github.com/igvteam/igv.js) open files on your own computer.

Web pages normally can't read local files. mini-server runs on your computer,
listens only on `127.0.0.1`, and serves files to a short list of allowed
websites. It can only read what you can already open yourself: your own disks
and any network shares you have mounted.

It's a single Python file with no dependencies.

## Quick start

You need Python 3.9 or later.

1. Download the latest zip from the
   [Releases](https://github.com/bioinfomethods/mini-server/releases) page and unzip it.
2. Start it:
   - **macOS:** double-click `start.command`. The first time, macOS may say it
     can't be opened because it's from an unidentified developer: right-click
     it, choose **Open**, then **Open** again. If macOS offers to install the
     "command line developer tools", accept; that installs Python.
   - **Windows:** double-click `start.bat`. If it says Python wasn't found,
     install it from [python.org](https://www.python.org/downloads/).
   - **Linux:** run `./start.sh` in a terminal.
3. A window opens saying mini-server is running. Leave it open while you use
   it; close it to stop.

To check it works, open <http://127.0.0.1:60152/> in your browser, paste the
full path of a BAM, CRAM or bgzipped VCF file (with its index alongside), and
click **Load in IGV**.

The first time a website uses mini-server, your browser may ask whether to
allow the site to access devices on your local network. Choose **Allow**.

## How websites use it

Files are addressed by their absolute path after `/f`:

| File                            | URL                                                     |
| ------------------------------- | ------------------------------------------------------- |
| `/Users/me/data/sample.bam`     | `http://127.0.0.1:60152/f/Users/me/data/sample.bam`     |
| `C:\data\sample.bam`            | `http://127.0.0.1:60152/f/C:/data/sample.bam`           |
| `\\server\share\sample.bam`     | `http://127.0.0.1:60152/f//server/share/sample.bam`     |

Percent-encode anything unusual in a path, and always encode `?`, `#` and `%`.
Range requests, `HEAD` and CORS preflights are supported.

`GET /health` returns `{"name": "mini-server", "version": "..."}`, so a page
can detect whether mini-server is running.

## Configuration

`config.json` next to the script:

| Key                  | Meaning                                                                                                                                                                   |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `port`               | Port to listen on (default `60152`).                                                                                                                                      |
| `allowed_origins`    | Websites allowed to read files, e.g. `https://viewer.example.org`. A leading `*.` matches any subdomain (not the bare domain). Without a port, any port matches. |
| `roots`              | If not empty, only files under these folders are served.                                                                                                                  |
| `allowed_extensions` | If not empty, only files with these endings are served. The default list covers common genomics formats.                                                                  |

`python3 mini_server.py --port 8123` overrides the port for one run.

## Security

- Listens only on `127.0.0.1`, so other machines can't connect.
- Cross-origin requests are refused unless the page's origin is in
  `allowed_origins`, so other websites you visit can't read your files.
- Requests whose `Host` isn't `127.0.0.1` or `localhost` are refused, which
  blocks DNS-rebinding attacks.
- Read-only: `GET`, `HEAD` and `OPTIONS` only, and no directory listings.
- Hidden files and folders (names starting with `.`, such as `~/.ssh`) are
  never served, and by default only genomics file types are.

Any page on an allowed website can read the files mini-server allows, so
keep `allowed_origins` to sites you trust, and consider setting `roots`.

## Development

```sh
python3 -m unittest discover -s tests -v
```

Pushing a tag such as `v0.1.0` builds the release zip and publishes a
GitHub release.
