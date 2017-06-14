#!/usr/bin/env python

import boto3
import getopt
import sys
import subprocess
import time

def main(argv):
    helptext = 'bluegreen.py -f <path to terraform project> -a <ami> -c <command> -t <timeout>'

    try:
        opts, args = getopt.getopt(argv,"hf:a:c:t:",["folder=","ami=","command=", "timeout="])
    except getopt.GetoptError:
        print helptext
        sys.exit(2)

    if opts:
        for opt, arg in opts:
            if opt == '-h':
                print helptext
                sys.exit(2)
            elif opt in ("-f", "--folder"):
                projectPath = arg
            elif opt in ("-a", "--ami"):
                ami = arg
            elif opt in ("-c", "--command"):
                command = arg
            elif opt in ("-t", "--timeout"):
                maxTimeout = int(arg)
    else:
        print helptext
        sys.exit(2)

    if 'command' not in locals():
        command = 'plan'

    if 'maxTimeout' not in locals():
        maxTimeout = 200

    if 'projectPath' not in locals():
        print 'Please give your folder path of your Terraform project'
        print helptext
        sys.exit(2)

    if 'ami' not in locals():
        print 'Please give a new AMI as argument'
        print helptext
        sys.exit(2)

    # Retrieve autoscaling group names
    agBlue = getTerraformOutput(projectPath, 'blue_asg_id')
    agGreen = getTerraformOutput(projectPath, 'green_asg_id')

    # Retrieve autoscaling groups information
    info = getAutoscalingInfo(agBlue, agGreen)

    # Determine the active autoscaling group
    active = getActive(info)

    # Bring up the not active autoscaling group with the new AMI
    desiredInstanceCount = newAutoscaling(info, active, ami, command, projectPath)

    # Retieve all ELBs annd ALBs
    elbs = getLoadbalancers(info, 'elb')
    albs = getLoadbalancers(info, 'alb')

    # Retrieve autoscaling groups information (we need to do this again because the launchconig has changed and we need this in a later phase)
    info = getAutoscalingInfo(agBlue, agGreen)

    if command == 'apply':
        print 'Waiting for 30 seconds to get autoscaling status'
        time.sleep(30)
        timeout = 30
        while checkScalingStatus(elbs, albs, desiredInstanceCount) != True:
            if timeout > maxTimeout:
                print 'Roling back'
                rollbackAutoscaling(info, active, ami, command, projectPath)
                sys.exit(2)

            print 'Waiting for 10 seconds to get autoscaling status'
            time.sleep(10)
            timeout += 10

        print 'We can stop the old autoscaling now'
        oldAutoscaling(info, active, ami, command, projectPath)

def getTerraformOutput (projectPath, output):
    process = subprocess.Popen('terraform output ' + output, shell=True, cwd=projectPath, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    output = process.communicate()[0].rstrip()
    return output

def getAutoscalingInfo (blue, green):
    client = boto3.client('autoscaling')
    response = client.describe_auto_scaling_groups(
        AutoScalingGroupNames=[
            blue,
            green,
        ],
        MaxRecords=2
    )
    return response

def getLoadbalancers(info, type):
    if type == 'alb':
        return info['AutoScalingGroups'][0]['TargetGroupARNs']
    else:
        return info['AutoScalingGroups'][0]['LoadBalancerNames']

def getAmi (launchconfig):
    client = boto3.client('autoscaling')
    response = client.describe_launch_configurations(
        LaunchConfigurationNames=[
            launchconfig,
        ],
        MaxRecords=1
    )
    return response['LaunchConfigurations'][0]['ImageId']

def getActive (info):
    if info['AutoScalingGroups'][0]['DesiredCapacity'] > 0 and info['AutoScalingGroups'][1]['DesiredCapacity'] == 0:
        print 'Blue is active'
        return 0
    elif info['AutoScalingGroups'][0]['DesiredCapacity'] == 0 and info['AutoScalingGroups'][1]['DesiredCapacity'] > 0:
        print 'Green is active'
        return 1
    else:
        print 'Both are active'
        sys.exit(1)

def newAutoscaling (info, active, ami, command, projectPath):
    blueMin = info['AutoScalingGroups'][active]['MinSize']
    blueMax = info['AutoScalingGroups'][active]['MaxSize']
    blueDesired = info['AutoScalingGroups'][active]['DesiredCapacity']

    greenMin = info['AutoScalingGroups'][active]['MinSize']
    greenMax = info['AutoScalingGroups'][active]['MaxSize']
    greenDesired = info['AutoScalingGroups'][active]['DesiredCapacity']

    if active == 0:
        blueAMI = getAmi(info['AutoScalingGroups'][active]['LaunchConfigurationName'])
        greenAMI = ami
    elif active == 1:
        blueAMI = ami
        greenAMI = getAmi(info['AutoScalingGroups'][active]['LaunchConfigurationName'])
    else:
        print 'No acive AMI'
        sys.exit(1)

    updateAutoscaling (command, blueMax, blueMin, blueDesired, blueAMI, greenMax, greenMin, greenDesired, greenAMI, projectPath)

    # Return the amount of instances we should see in ELBs and ALBs. This is * 2 because we need to think about both autoscaling groups.
    return info['AutoScalingGroups'][active]['DesiredCapacity'] * 2

def oldAutoscaling (info, active, ami, command, projectPath):
    blueAMI = getAmi(info['AutoScalingGroups'][0]['LaunchConfigurationName'])
    greenAMI = getAmi(info['AutoScalingGroups'][1]['LaunchConfigurationName'])
    if active == 0:
        blueMin = 0
        blueMax = 0
        blueDesired = 0

        greenMin = info['AutoScalingGroups'][active]['MinSize']
        greenMax = info['AutoScalingGroups'][active]['MaxSize']
        greenDesired = info['AutoScalingGroups'][active]['DesiredCapacity']
    elif active == 1:
        blueMin = info['AutoScalingGroups'][active]['MinSize']
        blueMax = info['AutoScalingGroups'][active]['MaxSize']
        blueDesired = info['AutoScalingGroups'][active]['DesiredCapacity']

        greenMin = 0
        greenMax = 0
        greenDesired = 0
    else:
        print 'No acive AMI'
        sys.exit(1)

    updateAutoscaling (command, blueMax, blueMin, blueDesired, blueAMI, greenMax, greenMin, greenDesired, greenAMI, projectPath)

def rollbackAutoscaling (info, active, ami, command, projectPath):
    blueAMI = getAmi(info['AutoScalingGroups'][0]['LaunchConfigurationName'])
    greenAMI = getAmi(info['AutoScalingGroups'][1]['LaunchConfigurationName'])


    if active == 0:
        blueMin = info['AutoScalingGroups'][0]['MinSize']
        blueMax = info['AutoScalingGroups'][0]['MaxSize']
        blueDesired = info['AutoScalingGroups'][0]['DesiredCapacity']

        greenMin = 0
        greenMax = 0
        greenDesired = 0
    elif active == 1:
        greenMin = info['AutoScalingGroups'][1]['MinSize']
        greenMax = info['AutoScalingGroups'][1]['MaxSize']
        greenDesired = info['AutoScalingGroups'][1]['DesiredCapacity']

        blueMin = 0
        blueMax = 0
        blueDesired = 0
    else:
        print 'No acive AMI'
        sys.exit(1)

    updateAutoscaling (command, blueMax, blueMin, blueDesired, blueAMI, greenMax, greenMin, greenDesired, greenAMI, projectPath)

def updateAutoscaling (command, blueMax, blueMin, blueDesired, blueAMI, greenMax, greenMin, greenDesired, greenAMI, projectPath):
    command = 'terraform ' + command + ' -var \'blue_max_size=' + str(blueMax) + '\' -var \'blue_min_size=' + str(blueMin) + '\' -var \'blue_desired_capacity=' + str(blueDesired) + '\' -var \'green_max_size=' + str(greenMax) + '\' -var \'green_min_size=' + str(greenMin) + '\' -var \'green_desired_capacity=' + str(greenDesired) + '\' -var \'green_ami=' + greenAMI + '\' -var \'blue_ami=' + blueAMI + '\''
    print command
    process = subprocess.Popen(command, shell=True, cwd=projectPath, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = process.communicate()
    print 'stdoutput'
    print out
    print 'stderror'
    print err

def checkScalingStatus (elbs, albs, desiredInstanceCount):
    client = boto3.client('elb')
    for elb in elbs:
        response = client.describe_instance_health(
            LoadBalancerName=elb
        )
        if desiredInstanceCount > len(response['InstanceStates']):
            print 'Not enough instances inside ELB, we expect ' + str(desiredInstanceCount) + ' and got ' + str(len(response['InstanceStates']))
            return False
        for state in response['InstanceStates']:
            print 'ELB: ' + state['State']
            if state['State'] != 'InService' :
                return False
    client = boto3.client('elbv2')
    for alb in albs:
        response = client.describe_target_health(
            TargetGroupArn=alb,
        )
        if desiredInstanceCount > len(response['TargetHealthDescriptions']):
            print 'Not enough instances inside ALB, we expect ' + str(desiredInstanceCount) + ' and got ' + str(len(response['TargetHealthDescriptions']))
            return False
        for state in response['TargetHealthDescriptions']:
            print 'ALB: ' + state['TargetHealth']['State']
            if state['TargetHealth']['State'] != 'healthy' :
                return False
    return True

if __name__ == "__main__":
   main(sys.argv[1:])