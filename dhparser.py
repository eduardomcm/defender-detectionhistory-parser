import sys
import os
import argparse
import io
import json
import binascii
import re
import time
from datetime import datetime, timedelta
from typing import final
sys.path.append("./")

# DEFINE COMMAND LINE ARGS   
arg_parser = argparse.ArgumentParser(description='Parse the contents of the given Windows Defender DetectionHistory file(s) into a readable format.')
arg_parser.version = "DetectionHistory Parser v1.0. Thank you for using my parser!"

arg_parser.add_argument('-f',
                        '--file',
                        action='store',
                        help='Path of the file/directory you are parsing.',
                        required=True)
arg_parser.add_argument('-g',
                        '--greedy',
                        action='store_true',
                        help='(CAUTION) Intended for use with recursive option. Will grab all files without an extension in a directory, even if they do not match Windows\' naming convention of DetectionHistory files.',
                        required=False)  
arg_parser.add_argument('-o',
                        '--output',
                        action='store',
                        help='Desired output folder name.',
                        required=True)    
arg_parser.add_argument('-r',
                        '--recursive',
                        action='store_true',
                        help='Recursive input. MUST SPECIFY if you need to parse a (sub)directory of DetectionHistory files. Only picks up files which match Windows\' naming convention of DetectionHistory files.',
                        required=False)     
arg_parser.add_argument('-s',
                        '--silent',
                        action='store_true',
                        help='Silences output to command line, including non-critical warnings. Confirmation of found files, finished processing and critical errors will still be shown.',
                        required=False)       
arg_parser.add_argument('-v',
                        '--version',
                        action='version',
                        help='Displays the version of the application and exits.')


def byte_swap_to_int(hexval, str_flag=False):
    # Reads in bytes, swaps endianness, and converts to an integer if needed.
    # str_flag gives us the value to return an endian-swapped, hex-string representation of a value. For some fields, this is more appropriate.

    hex_int = 0
    hexval = [hexval[i:i+2] for i in range(0, len(hexval), 2)] # need to swap endianness of filetime
    hexval.reverse()
    hexval = b''.join(hexval)
    if str_flag: # stopping point if only hex-string needed
        return hexval.decode('utf-8')
    hexval = [int("0x"+(hexval[i:i+1].decode('utf-8')),16) for i in range(0, len(hexval), 1)]
    exponent = len(hexval)-1
    while exponent>=0:
        hex_int += hexval[abs((len(hexval)-1)-exponent)]*(pow(16,exponent))
        exponent += -1

    return hex_int

def parse_header_and_guid(file):
    # Validates that file is a proper DetectionHistory file, and returns parsed GUID value located directly after header.

    header = file.read(6) # 6 = known DetectionHistory header size
    if header != b'\x08\x00\x00\x00\x08\x00': # check file header against known valid DetectionHistory file header
        print("Invalid DetectionHistory file!")
        raise(IOError)
    file.read(18) # skipping over some zeroes, and an unknown 3-byte sequence between offset 08-0A
    guid_oct = list()
    guid_oct.append(binascii.hexlify(file.read(4)))
    guid_oct.append(binascii.hexlify(file.read(2)))
    guid_oct.append(binascii.hexlify(file.read(2)))
    guid_oct.append(binascii.hexlify(file.read(2))) # this hex is not flipped in file data
    guid_oct.append(binascii.hexlify(file.read(6))) # this hex is not flipped in file data
    
    oct_count = 0
    while oct_count<=2:
        oct = guid_oct[oct_count]
        newlist = [oct[i:i+2] for i in range(0, len(oct), 2)]
        newlist.reverse()
        guid_oct[oct_count] = b''.join(newlist)
        oct_count += 1

    guid_final = (guid_oct[0].decode('utf-8')+"-"
        +guid_oct[1].decode('utf-8')+"-"
        +guid_oct[2].decode('utf-8')+"-"
        +guid_oct[3].decode('utf-8')+"-"
        +guid_oct[4].decode('utf-8'))

    return guid_final


def parse_filetime(file):
    # Parse known filetime hex string to a readable timestamp.

    filetime = ""
    filetime_nanoseconds = 0
    file.read(4) # skip ahead known distance between "Time" field and FILETIME timestamp
    filetime = binascii.hexlify(file.read(8))
    filetime_nanoseconds = byte_swap_to_int(filetime)
    filetime_epoch = timedelta(microseconds=float(filetime_nanoseconds/10)) # time represented in hundreds of nanoseconds
    filetime_date = datetime(1601, 1, 1)+filetime_epoch # FILETIME begins on Jan 1, 1601
    return filetime_date.strftime("%m-%d-%Y %H:%M:%S")


def parse_unmapped_value(file):
    # Decoding bytes of high hex vals can result in unmapped chars. Returns bytes of some unknown hex string.
    # Based on Windows specs, ThreatID should be an Int64, though this does not always seem to be the case.
    # https://docs.microsoft.com/en-us/powershell/module/defender/get-mpthreatdetection?view=windowsserver2022-ps
    # https://www.windows-security.org/c328023496d244ced5d0c4445e4f1806/threat-id-exclusions

    unmapped_val = ""
    chunk = file.read(1) # initial byte to perform checks off of in loop
    while True: # loop that checks for beginning of unmapped value
        if chunk==b'\x00': 
            chunk = chunk+file.read(1)
            if chunk==b'\x00\x00': 
                chunk = chunk+file.read(1)
                if chunk==b'\x00\x00\x00': 
                    break
        else:
            chunk = file.read(1) # go to next byte

    caution_sequences = [b'\x00', b'\x32', b'\x24', b'\x04', b'\x06'] # hex bytes observed in file which are known to delimit data from empty bytes
    unmapped_val = file.read(3) # Windows should always allocate at least this much for an unknown hex string in this file.
    chunk = file.read(1)
    while True: # loop to read in rest of unmapped value, as well as check for ending point
        while chunk in caution_sequences:
            next_chunk = file.read(1)
            if next_chunk==b'\x00':
                return binascii.hexlify(unmapped_val)
            chunk = next_chunk
        # progress to this code if chunk not in 'caution_sequences'            
        unmapped_val += chunk
        chunk = file.read(1)
    
    return 0 # should never get to this point


def parse_detection_history(user_in, user_out):
    # Main function to parse given DetectionHistory file and write output fields to a readable file.

    filepath = user_in[0] # file directory itself
    outfile_name = user_in[1] # filename used for outfile writing
    outfolder = user_out
    parsed_value_dict = dict()

    # DEFINE MODES
    KEY_READ_MODE = b'\x00'
    VALUE_READ_MODE = b'\x01'
    NULL_DATA_MODE = b'\x02'
    CURRENT_MODE = KEY_READ_MODE
    LAST_READ_MODE = b'\xFF'
    # DEFINE SECTIONS
    MAGIC_VERSION_SECTION = b'\x04'
    GENERAL_SECTION = b'\x05'
    NEAREST_EOF_SECTION = b'\x06'
    # EXTRA NEEDED VARIABLES
    EOF_SECTION_KEYS = ["User","SpawningProcessName","SecurityGroup"] # Fields in this section not explicity defined in file, so fields named off based off current knowledge
    CURRENT_EOF_SECTION_KEY = -1

    with open(filepath, 'rb') as f:
        parsed_value_dict["GUID"] = parse_header_and_guid(f)
        f.read(8) # skipping over some empty space
        temp_key = ""

        while True:
            while MAGIC_VERSION_SECTION:
                chunk = f.read(2)
                if not chunk:
                    print("End of section or file detected. Moving on...")
                    MAGIC_VERSION_SECTION = 0 # break out of this section
                    break
                if CURRENT_MODE==KEY_READ_MODE: 
                    if chunk==b'\x3A\x00': # first few sections are delimited by a Windows-1252 colon rather than multiple \x00 bytes
                        temp_key = re.sub("\x00", "", temp_key)
                        parsed_value_dict[temp_key] = "" # we will reset temp_key after setting the value
                        CURRENT_MODE = VALUE_READ_MODE 
                    else:
                        temp_key = temp_key+chunk.decode('windows-1252')
                        if "f\x00i\x00l\x00e" in temp_key: # file key/value pair signifies end of values delimited by colons (or \x3A)
                            print("End of Magic Version section!")
                            temp_key = re.sub("\x00", "", temp_key)
                            parsed_value_dict[temp_key] = "" # make sure "file" key gets assigned
                            CURRENT_MODE = VALUE_READ_MODE
                            MAGIC_VERSION_SECTION = 0 # break out of this section
                            f.read(16) # skip some zeroes to next section
                elif CURRENT_MODE==VALUE_READ_MODE:
                    if chunk==b'\x00\x00': # if chunk to be read is empty
                        if f.read(2)==b'\x00\x00': # if next chunk empty as well
                            parsed_value_dict[temp_key] = re.sub("\x00", "", parsed_value_dict[temp_key]) # finalize value
                            temp_key = "" # reset temp key for next run
                            CURRENT_MODE=NULL_DATA_MODE
                    else:
                        parsed_value_dict[temp_key] += chunk.decode('windows-1252')
                elif CURRENT_MODE==NULL_DATA_MODE:
                    if f.tell()==242:
                        parsed_value_dict["ThreatStatusID"] = byte_swap_to_int(binascii.hexlify(chunk))
                    if len(re.sub(r'\W+', '', chunk.decode('windows-1252')))>=1: # regex function removes all non-alphanum characters
                        chunk = chunk+f.read(2) # double check if there are 2 alphanum chars in sequence. sometimes there are isolated, irrelevant hex values in file which are encodable chars
                        if len(re.sub(r'\W+', '', chunk.decode('windows-1252')))>=2: 
                            temp_key += chunk.decode('windows-1252')
                            CURRENT_MODE = KEY_READ_MODE

            while GENERAL_SECTION:
                chunk = f.read(2)
                if not chunk:
                    print("End of section or file detected. Moving on...")
                    GENERAL_SECTION = 0 # break out of this section
                    break
                elif CURRENT_MODE==NULL_DATA_MODE:
                    if len(re.sub(r'\W+', '', chunk.decode('windows-1252')))>=1: # regex function removes all non-alphanum characters
                        chunk = chunk+f.read(2) # double check if there are 2 alphanum chars in sequence. sometimes there are isolated, irrelevant hex values in file which are encodable chars
                        if len(re.sub(r'\W+', '', chunk.decode('windows-1252')))>=2: 
                            if LAST_READ_MODE==KEY_READ_MODE: # we need to switch back and forth between key and value reading
                                parsed_value_dict[temp_key] += chunk.decode('windows-1252')
                                CURRENT_MODE=VALUE_READ_MODE
                            else:
                                temp_key += chunk.decode('windows-1252')
                                CURRENT_MODE=KEY_READ_MODE
                    elif chunk==b'\x0A\x00' or chunk==b'\x00\x0A':
                        f.read(2)
                        chunk = f.read(4)
                        if chunk==b'\x15\x00\x00\x00' or chunk==chunk==b'\x00\x15\x00\x00': # false positives
                            continue
                        print("End of General Section!")
                        f.read(4) # skip over unneeded section of hex
                        GENERAL_SECTION = 0 # break out of this section
                else: # Applies to KEY_READ or VALUE_READ mode
                    if chunk==b'\x00\x00': # Check to switch to NULL_MODE must happen in either KEY_READ or VALUE_READ mode          
                        if CURRENT_MODE==KEY_READ_MODE:
                            temp_key = re.sub("\x00", "", temp_key)
                            if "Magic." in temp_key[0:6]:
                                print("Extraneous \"Magic Version\" key detected! Continuing...")
                                temp_key = "" # reset for next KEY_READ_MODE run
                                LAST_READ_MODE = VALUE_READ_MODE # skip over this key, read in next key
                            elif "Time" in temp_key:
                                parsed_value_dict[temp_key] = parse_filetime(f)
                                temp_key = "" # reset for next KEY_READ_MODE run
                                LAST_READ_MODE = VALUE_READ_MODE # value just set, read in next key
                            elif "ThreatTrackingThreatId" in temp_key or "ThreatTrackingSize" in temp_key:
                                parsed_value_dict[temp_key] = byte_swap_to_int(parse_unmapped_value(f))   
                                temp_key = "" # reset for next KEY_READ_MODE run
                                LAST_READ_MODE = VALUE_READ_MODE # value just set, read in next key         
                            elif "ThreatTrackingSigSeq" in temp_key:
                                parsed_value_dict[temp_key] = "0x0000"+byte_swap_to_int(parse_unmapped_value(f), str_flag=True)   
                                temp_key = "" # reset for next KEY_READ_MODE run
                                LAST_READ_MODE = VALUE_READ_MODE # value just set, read in next key                   
                            else:
                                parsed_value_dict[temp_key] = "" # we will reset temp_key after setting the value in VALUE_READ_MODE
                                LAST_READ_MODE = KEY_READ_MODE
                            CURRENT_MODE = NULL_DATA_MODE
                        elif CURRENT_MODE==VALUE_READ_MODE:
                            final_value = re.sub("\x00", "", parsed_value_dict[temp_key])
                            if "Threat" in final_value[0:6] or "regkey" in final_value[0:6]: # check for values that should be keys
                                print(f"Irregularity in file/empty value caused skip in parsing for keys \"{temp_key}\" and \"{final_value}\". Configuring...")
                                parsed_value_dict[temp_key] = "" # reset extraneous value for temp_key
                                parsed_value_dict[final_value] = "" # this value containing "Threat" or "regkey" should have been a key
                                temp_key = final_value # set the final_value to the new key
                                LAST_READ_MODE = KEY_READ_MODE # for that key, collect the next value
                                if "ThreatTrackingThreatId" in temp_key or "ThreatTrackingSize" in temp_key:
                                    parsed_value_dict[temp_key] = byte_swap_to_int(parse_unmapped_value(f))  
                                    temp_key = "" # reset for next KEY_READ_MODE run
                                    LAST_READ_MODE = VALUE_READ_MODE
                            else: # when working as intended
                                parsed_value_dict[temp_key] = final_value # finalize value
                                LAST_READ_MODE = VALUE_READ_MODE 
                                temp_key = "" # reset temp key for next KEY_READ_MODE
                            CURRENT_MODE = NULL_DATA_MODE
                    elif CURRENT_MODE==KEY_READ_MODE:
                        temp_key += chunk.decode('windows-1252')
                    elif CURRENT_MODE==VALUE_READ_MODE:
                        parsed_value_dict[temp_key] += chunk.decode('windows-1252')

            while NEAREST_EOF_SECTION:
                chunk = f.read(2)
                if not chunk: # indicates EOF
                    NEAREST_EOF_SECTION = 0 # break out of this section
                    break
                elif CURRENT_MODE==NULL_DATA_MODE:
                    if chunk==b'\x0A\x00' or chunk==b'\x00\x0A': # track lines with "\x0A" bytes. This byte delimits an unknown hex sequence that (for now) we can skip
                        f.read(10)
                        continue
                    try:
                        if len(re.sub(r'\W+', '', chunk.decode('windows-1252')))>=1: # regex function removes all non-alphanum characters
                            chunk = chunk+f.read(2) # double check if there are 2 alphanum chars in sequence. sometimes there are isolated, irrelevant hex values in file which are encodable chars
                            if len(re.sub(r':(?=..(?<!\d:\d\d))|[^a-zA-Z0-9 ](?<!:)', '', chunk.decode('windows-1252')))>=2 and b'\x00' in chunk: # ensure colons are treated as alphanum chars with regex
                                CURRENT_EOF_SECTION_KEY=CURRENT_EOF_SECTION_KEY+1 
                                parsed_value_dict[EOF_SECTION_KEYS[CURRENT_EOF_SECTION_KEY]] = chunk.decode('windows-1252')
                                CURRENT_MODE=VALUE_READ_MODE
                    except UnicodeDecodeError as e:
                        print(f"||{e}|| caught for bytes {chunk} : Unreadable hex pattern identified. Continuing...")
                elif CURRENT_MODE==VALUE_READ_MODE:                  
                    if chunk==b'\x00\x00': # Check to switch to NULL_MODE 
                        parsed_value_dict[EOF_SECTION_KEYS[CURRENT_EOF_SECTION_KEY]] = re.sub("\x00", "", parsed_value_dict[EOF_SECTION_KEYS[CURRENT_EOF_SECTION_KEY]])       
                        CURRENT_MODE=NULL_DATA_MODE
                    else:
                        parsed_value_dict[EOF_SECTION_KEYS[CURRENT_EOF_SECTION_KEY]] += chunk.decode('windows-1252')
            
            break 

        print(f"EOF in \"{filepath}\" detected!")
        if not os.path.exists(outfolder):
            os.makedirs(outfolder)
        with open(f"{outfolder}\\{outfile_name}.json", 'w') as out:
            json.dump(parsed_value_dict, out, indent=4)
        print("\n---------------------------------------")
                        


def main():
    print("\n---------------------------------------\nDetectionHistory Parser v1.0 by Jordan Klepser\nhttps://github.com/jklepsercyber/\n---------------------------------------\n")
    args = arg_parser.parse_args()
    list_files = []
    error_count = 0
    silent = io.StringIO()
    sys.stdout = silent if args.silent else sys.__stdout__

    if args.recursive and os.path.isdir(args.file): # search directory for DetectionHistory files
        for directory, dirnames, files in os.walk(args.file): # dirnames unused, but needs to be included for python's sake
            for name in files:
                if not "." in name and args.greedy: # grabs all files without extension
                    list_files.append([os.path.join(directory, name), name]) # save name, so we don't have to regex out filename during outfile writing later on
                elif not "." in name and re.match(r'[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', name): # regex matches default DetectionHistory file naming convention
                    list_files.append([os.path.join(directory, name), name]) # save name, so we don't have to regex out filename during outfile writing later on
    elif os.path.isfile(args.file):
        list_files.append(args.file)
    elif os.path.isdir(args.file):
        sys.stdout = sys.__stdout__
        print(f"You specified \"{args.file}\", which is a directory. Please specify the -r argument if you wish to parse a directory, and try again. \n")
        return        
    else:
        sys.stdout = sys.__stdout__
        print(f"Path \"{args.file}\" is not a valid file or directory. Please validate the desired input and try again. \n")
        return

    for f in list_files:
        try:
            if len(list_files)==1:
                parse_detection_history([os.path.normpath(args.file), os.path.basename(args.file)], args.output)
                break
            else:
                sys.stdout = sys.__stdout__
                print(f"Found DetectionHistory file \"{f[1]}\".")
                sys.stdout = silent if args.silent else sys.__stdout__
                parse_detection_history(f, args.output)
        except Exception as e:
            sys.stdout = sys.__stdout__
            print(f"ERROR: ||{e}|| caught in {f[0]}. Moving on to next file...\n---------------------------------------")
            error_count+=1
            sys.stdout = silent if args.silent else sys.__stdout__

    sys.stdout = sys.__stdout__
    print(f"{len(list_files)-error_count} of {len(list_files)} DetectionHistory files found were successfully parsed, with output written to \"{args.output}\" in {time.process_time()} seconds.\n---------------------------------------")

if __name__ == "__main__":
    main()
